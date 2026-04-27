"""APScheduler wiring + the snapshot jobs.

A-share trading hours: 09:30-11:30, 13:00-15:00 (Asia/Shanghai).

Two snapshot tiers run concurrently:

  - **Quotes (5min)**: bulk eastmoney pull of price/change/volume/turnover
    + main fund flow for every watched code in a single round-trip. Light,
    fast, and the only thing the 盯盘 list actually needs to feel live.
  - **Full (hourly)**: per-code akshare fan-out for news/notices, plus the
    post-close 龙虎榜 pass. Heavy but rare. Carried over by the next quotes
    tick so the latest snapshot row always has the most recent context
    fields filled in.

Daily LLM analysis pass runs once at 09:35 so verdicts are fresh on arrival.
The scheduler runs in-process; Railway must be pinned to 1 backend replica.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Analysis, Snapshot, Watchlist
from .analysis import generate as analysis_generate, get_cached as analysis_cached
from .scraper import collect_lhb_today, collect_many, collect_quotes_bulk
from .signals import compute_signals

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")

CRON_TIMES = [
    {"hour": 9, "minute": 30},
    {"hour": 10, "minute": 30},
    {"hour": 11, "minute": 30},
    {"hour": 14, "minute": 0},
    {"hour": 15, "minute": 0},
    {"hour": 16, "minute": 0},  # post-close: includes LHB
]

scheduler: BackgroundScheduler | None = None


def run_snapshot_job(post_close: bool = False) -> dict:
    """Pull snapshots for every watched code; insert into DB.

    Returns a small summary dict used by the manual-trigger endpoint.
    `post_close=True` additionally pulls today's 龙虎榜.
    """
    db: Session = SessionLocal()
    try:
        codes = [w.code for w in db.query(Watchlist.code).all()]
        if not codes:
            logger.info("snapshot job: watchlist empty, skipping")
            return {"codes": 0, "inserted": 0}

        logger.info("snapshot job: collecting %d codes (post_close=%s)", len(codes), post_close)
        snaps = collect_many(codes)

        lhb_map = collect_lhb_today() if post_close else {}

        inserted = 0
        for s in snaps:
            if s["code"] in lhb_map:
                s["lhb"] = lhb_map[s["code"]]
            s["signals"] = compute_signals(s)
            row = Snapshot(
                code=s["code"],
                price=s.get("price"),
                change_pct=s.get("change_pct"),
                volume=s.get("volume"),
                turnover=s.get("turnover"),
                main_net_flow=s.get("main_net_flow"),
                north_hold_change=s.get("north_hold_change"),
                signals=s.get("signals") or [],
                news=s.get("news") or [],
                notices=s.get("notices") or [],
                lhb=s.get("lhb"),
            )
            db.add(row)
            inserted += 1
        db.commit()
        logger.info("snapshot job: inserted %d rows", inserted)
        return {"codes": len(codes), "inserted": inserted, "post_close": post_close}
    except Exception:
        db.rollback()
        logger.exception("snapshot job failed")
        raise
    finally:
        db.close()


def _scheduled_tick(post_close: bool):
    try:
        run_snapshot_job(post_close=post_close)
    except Exception:
        # already logged; don't let APScheduler kill the job permanently
        pass


def _is_trading_minute(now: datetime) -> bool:
    """A-share continuous-trading window in Asia/Shanghai time."""
    h, m = now.hour, now.minute
    morning = (h == 9 and m >= 30) or (h == 10) or (h == 11 and m <= 30)
    afternoon = (h == 13) or (h == 14) or (h == 15 and m == 0)
    return morning or afternoon


def _carry_forward(latest: Snapshot | None) -> dict:
    """Pull context fields (news/notices/lhb) AND the prior main_net_flow
    from the most recent snapshot for this code.

    Quotes ticks usually don't refetch news/notices/lhb at all (heavy
    per-code fan-out), and main_net_flow is flaky (sina doesn't carry it
    and the per-code akshare fund_flow fails intermittently on Railway).
    Carrying the previous value forward keeps the 盯盘 list visually
    stable instead of blinking to – every time one source is down.
    """
    if latest is None:
        return {
            "news": [], "notices": [], "lhb": None, "prev_main_net_flow": None,
        }
    return {
        "news": latest.news or [],
        "notices": latest.notices or [],
        "lhb": latest.lhb,
        "prev_main_net_flow": latest.main_net_flow,
    }


def run_quotes_job() -> dict:
    """Bulk-pull price + main flow for the whole watchlist in one shot.

    Writes a snapshot row per code, copying news/notices/lhb forward from
    the previous snapshot so signal detection and the detail view keep
    seeing the latest context fields without the heavy per-code fan-out.
    """
    db: Session = SessionLocal()
    try:
        codes = [w.code for w in db.query(Watchlist.code).all()]
        if not codes:
            return {"codes": 0, "inserted": 0, "tier": "quotes"}

        logger.info("quotes job: bulk pulling %d codes", len(codes))
        bulk = collect_quotes_bulk(codes)

        # If the bulk endpoints handed back nothing at all, abort instead of
        # writing 22 empty rows that would shadow the last good snapshot in
        # the 盯盘 list. Caller logs already contain the akshare failure.
        if not any(bulk.values()):
            logger.warning(
                "quotes job: bulk returned no data for any of %d codes; "
                "skipping insert to preserve last good snapshot",
                len(codes),
            )
            return {"codes": len(codes), "inserted": 0, "tier": "quotes",
                    "skipped": "bulk_empty"}

        # Load each code's latest snapshot once to carry context forward.
        latest_by_code: dict[str, Snapshot] = {}
        for code in codes:
            row = (
                db.query(Snapshot)
                .filter(Snapshot.code == code)
                .order_by(Snapshot.id.desc())
                .first()
            )
            if row is not None:
                latest_by_code[code] = row

        inserted = 0
        skipped = 0
        for code in codes:
            quote = bulk.get(code) or {}
            # Skip codes the bulk endpoints didn't cover this tick — writing
            # a quote-less row here would replace a perfectly good prior
            # snapshot with nulls (e.g., that's what produced the 13:30
            # all-blank state). Caller still has the previous row to display.
            has_core = any(
                quote.get(k) is not None
                for k in ("price", "change_pct", "main_net_flow")
            )
            if not has_core:
                skipped += 1
                continue
            carry = _carry_forward(latest_by_code.get(code))
            # Use the freshly fetched main_net_flow if we got one; otherwise
            # carry the previous tick's value forward so the UI doesn't
            # flicker when fund-flow is the flaky source.
            net_flow = quote.get("main_net_flow")
            if net_flow is None:
                net_flow = carry.pop("prev_main_net_flow")
            else:
                carry.pop("prev_main_net_flow", None)
            snap = {
                "code": code,
                "price": quote.get("price"),
                "change_pct": quote.get("change_pct"),
                "volume": quote.get("volume"),
                "turnover": quote.get("turnover"),
                "main_net_flow": net_flow,
                "north_hold_change": None,
                **carry,
            }
            snap["signals"] = compute_signals(snap)
            row = Snapshot(
                code=code,
                price=snap["price"],
                change_pct=snap["change_pct"],
                volume=snap["volume"],
                turnover=snap["turnover"],
                main_net_flow=snap["main_net_flow"],
                north_hold_change=None,
                signals=snap["signals"],
                news=snap["news"],
                notices=snap["notices"],
                lhb=snap["lhb"],
            )
            db.add(row)
            inserted += 1
        db.commit()
        logger.info("quotes job: inserted %d rows, skipped %d", inserted, skipped)
        return {"codes": len(codes), "inserted": inserted, "skipped_codes": skipped,
                "tier": "quotes"}
    except Exception:
        db.rollback()
        logger.exception("quotes job failed")
        raise
    finally:
        db.close()


def _quotes_tick():
    # APScheduler fires us every 5 min during 09:00–14:55 mon-fri, but only
    # ~49 of those slots are actually in-session. Skip the rest so we don't
    # bloat snapshots with no-op rows during lunch break / pre-open.
    if not _is_trading_minute(datetime.now(SHANGHAI)):
        return
    try:
        run_quotes_job()
    except Exception:
        pass  # already logged


def run_daily_analysis_job(only_stale: bool = True, only_missing: bool = False) -> dict:
    """Generate (and cache) LLM analyses for the watchlist.

    Two skip modes (mutually exclusive in spirit; `only_missing` wins when both
    are True):

    - `only_missing=True`: skip any code that already has a v2-schema cached
      analysis row, *regardless of age*. Use this for the manual "批量解析"
      button when there are 待生成 rows — we just want to fill in the gaps,
      not re-burn tokens on rows the user already has.

    - `only_stale=True` (default): skip any code whose cached analysis is
      still within the 4h freshness window. The 09:35 cron uses this — the
      previous day's rows are >24h old by morning so they all repaint.

    Pass both False to force-regenerate every code (e.g., the manual button
    falling through to "全部重新解析" when 待生成 == 0).

    Runs serially. One bad code doesn't sink the rest. If ANTHROPIC_API_KEY
    isn't set we log and skip, so the job is harmless without a key.
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.info("daily analysis job: ANTHROPIC_API_KEY not set, skipping")
        return {"codes": 0, "generated": 0, "failed": 0, "skipped": 0,
                "no_api_key": True}

    db: Session = SessionLocal()
    try:
        codes = [w.code for w in db.query(Watchlist.code).all()]
        if not codes:
            logger.info("daily analysis job: watchlist empty, skipping")
            return {"codes": 0, "generated": 0, "failed": 0, "skipped": 0}

        logger.info(
            "daily analysis job: %d codes, only_missing=%s only_stale=%s",
            len(codes), only_missing, only_stale,
        )
        generated = 0
        failed = 0
        skipped = 0
        for code in codes:
            if only_missing:
                # "Has any v2 cached row" — same shape check get_cached uses
                # for schema-version invalidation.
                row = db.query(Analysis).filter(Analysis.code == code).first()
                has_v2 = (
                    row is not None
                    and isinstance(row.key_table, dict)
                    and "company_tag" in row.key_table
                )
                if has_v2:
                    skipped += 1
                    continue
            elif only_stale and analysis_cached(db, code) is not None:
                skipped += 1
                continue
            try:
                analysis_generate(db, code)
                generated += 1
            except Exception:
                failed += 1
                logger.exception("daily analysis job: %s failed", code)
        logger.info(
            "daily analysis job: generated=%d failed=%d skipped=%d",
            generated, failed, skipped,
        )
        return {
            "codes": len(codes),
            "generated": generated,
            "failed": failed,
            "skipped": skipped,
        }
    finally:
        db.close()


def _daily_analysis_tick():
    try:
        run_daily_analysis_job()
    except Exception:
        # already logged; don't let APScheduler kill the job permanently
        pass


def start_scheduler() -> None:
    """Idempotent. Called from FastAPI lifespan."""
    global scheduler
    if scheduler is not None:
        return
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    for spec in CRON_TIMES:
        post_close = (spec["hour"] == 16)
        sched.add_job(
            _scheduled_tick,
            CronTrigger(day_of_week="mon-fri", **spec, timezone="Asia/Shanghai"),
            kwargs={"post_close": post_close},
            id=f"snap_{spec['hour']:02d}_{spec['minute']:02d}",
            replace_existing=True,
            misfire_grace_time=600,
        )
    # Daily analysis pass at 09:35 — runs ~5 min after the open snapshot so
    # it consumes today's 09:30 data. mon-fri only.
    sched.add_job(
        _daily_analysis_tick,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=35, timezone="Asia/Shanghai"),
        id="daily_analysis_09_35",
        replace_existing=True,
        misfire_grace_time=1800,  # 30min — analysis is fine even if scheduler was late
    )
    # 5-min quotes pass during the trading window. APScheduler doesn't have a
    # "trading hours" concept so we cast a slightly wider net (09:00–14:55
    # mon-fri) and the tick function itself short-circuits outside 09:30–11:30
    # / 13:00–15:00.
    sched.add_job(
        _quotes_tick,
        CronTrigger(
            day_of_week="mon-fri", hour="9-14", minute="*/5",
            timezone="Asia/Shanghai",
        ),
        id="quotes_5min",
        replace_existing=True,
        misfire_grace_time=120,
    )
    sched.start()
    scheduler = sched
    logger.info(
        "scheduler started: %d full-snapshot jobs + 1 quotes job + 1 analysis job",
        len(CRON_TIMES),
    )


def stop_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None
