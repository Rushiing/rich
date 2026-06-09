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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, ensure_extra_columns
from ..models import Analysis, Snapshot, Watchlist
from .analysis import (
    generate as analysis_generate,
    get_cached as analysis_cached,
    should_reanalyze,
)
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed

from .scraper import (
    VALUATION_FIELDS, collect_lhb_today, collect_many, collect_one,
    collect_quotes_bulk,
)
from .realtime_quotes import fetch_quotes_sina, fetch_quotes_tencent
from .signals import compute_signals
from . import kline as kline_svc, three_day, industry as industry_svc

# Phase 7: extra fields the snapshot row carries beyond the legacy set.
# Listed here so worker / row builders / carry-forward stay in sync.
THREE_DAY_FIELDS = ("change_pct_3d", "turnover_rate_3d", "net_flow_3d")
INDUSTRY_FIELDS = (
    "industry_name", "industry_pe_pctile", "industry_change_3d_pctile",
    "industry_flow_3d_pctile", "industry_pe_avg", "industry_pb_avg",
)

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


def _snapshot_worker(code: str, bulk_spot: dict | None, lhb_map: dict) -> bool:
    """Per-stock pipeline: collect remaining akshare fields + write own row.

    Critical layout: the slow akshare fan-out (5-30s) runs WITHOUT a DB
    connection held. The Session is opened only for the milliseconds-long
    insert + commit. Otherwise 10 concurrent workers would each park a
    connection for the full slow window, saturating the pool.

    `bulk_spot` carries the pre-fetched fields the worker doesn't need to
    refetch — Tencent quote, 3-day metrics, industry context. The slow
    phase only runs news / notices / fund_flow per code.

    Returns True on persisted, False on collection or DB failure.
    """
    # --- slow phase: NO DB session held ---
    try:
        s = collect_one(code, bulk_spot=bulk_spot)
    except Exception:
        logger.exception("snapshot worker: collect_one failed for %s", code)
        return False
    if code in lhb_map:
        s["lhb"] = lhb_map[code]
    s["signals"] = compute_signals(s)

    # --- fast phase: hold a connection only for the write ---
    db: Session = SessionLocal()
    try:
        row = Snapshot(
            code=code,
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
            **{f: s.get(f) for f in VALUATION_FIELDS},
            **{f: s.get(f) for f in THREE_DAY_FIELDS},
            **{f: s.get(f) for f in INDUSTRY_FIELDS},
        )
        db.add(row)
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("snapshot worker: DB write failed for %s", code)
        return False
    finally:
        db.close()


def _enrich_with_three_day_and_industry(
    codes: list[str], bulk: dict[str, dict],
) -> dict[str, dict]:
    """Layer the cheap-to-fetch context fields onto the bulk-quote dicts:
    3-day rolling metrics + industry name + per-industry percentiles +
    per-industry averages. Returns the enriched bulk in place (also
    returns it for chaining).

    This runs once per job on the main thread BEFORE workers fan out so
    each worker just spreads it into its row constructor. Percentiles
    require seeing the whole watchlist pool, so it must be a single pass.
    """
    # 3-day metrics (cached, ~30min TTL inside three_day module)
    metrics_3d = three_day.get_metrics(codes)
    for code, m in metrics_3d.items():
        bulk.setdefault(code, {}).update(m)

    # Industry names from our cached industry_meta table
    industry_map = industry_svc.get_industry_map(codes)

    # Build a lightweight snap-list view for percentile compute. We need
    # pe_ratio (already in bulk from Tencent) + change_pct_3d / net_flow_3d
    # / pb_ratio. industry_svc.compute_industry_context mutates this list
    # adding the 6 industry_* fields.
    pool = [
        {
            "code": c,
            "pe_ratio": bulk.get(c, {}).get("pe_ratio"),
            "pb_ratio": bulk.get(c, {}).get("pb_ratio"),
            "change_pct_3d": bulk.get(c, {}).get("change_pct_3d"),
            "net_flow_3d": bulk.get(c, {}).get("net_flow_3d"),
        }
        for c in codes
    ]
    industry_svc.compute_industry_context(pool, industry_map)
    for entry in pool:
        for f in INDUSTRY_FIELDS:
            bulk.setdefault(entry["code"], {})[f] = entry.get(f)

    # Phase 9: attach latest K-line indicators so signal predicates that
    # rely on technical state (breakout_20d / below_ma60 / MACD crosses)
    # can fire. Stored under "kline" — never written to the snapshot row,
    # consumed only by compute_signals.
    indicators = kline_svc.latest_indicators_for_codes(codes)
    for code, ind in indicators.items():
        bulk.setdefault(code, {})["kline"] = ind

    return bulk


def run_snapshot_job(post_close: bool = False) -> dict:
    """Pull snapshots for every watched code; insert into DB.

    Architecture: pre-fetch the cheap bulk sources (Tencent + sina + LHB)
    on the main thread, then fan out 10 worker threads — each runs the
    slow per-stock akshare calls (news / notices / fund_flow) AND commits
    its own row immediately on success. Result: the user sees rows
    appearing as each worker finishes, not a 5-minute stall followed by
    a bulk commit. Equally important, a SIGTERM mid-job preserves every
    row that managed to commit before the kill.

    `post_close=True` additionally pulls today's 龙虎榜.
    """
    # Self-heal: if the post-MVP columns weren't added at lifespan startup,
    # retry now. Idempotent — checks information_schema before each ALTER.
    ensure_extra_columns()

    # Cheap upfront work: read watchlist (short tx), then the two bulk
    # quote sources. Both are single HTTP round-trips, ~2s combined.
    db: Session = SessionLocal()
    try:
        codes = [w.code for w in db.query(Watchlist.code).all()]
    finally:
        db.close()

    if not codes:
        logger.info("snapshot job: watchlist empty, skipping")
        return {"codes": 0, "inserted": 0}

    logger.info("snapshot job: starting %d codes (post_close=%s)", len(codes), post_close)

    bulk: dict[str, dict] = {}
    tencent = fetch_quotes_tencent(codes)
    if tencent:
        bulk.update(tencent)
        logger.info("snapshot job: tencent filled %d/%d", len(tencent), len(codes))
    needs_sina = [c for c in codes if c not in bulk]
    if needs_sina:
        sina = fetch_quotes_sina(needs_sina)
        for c, q in sina.items():
            bulk.setdefault(c, {}).update(q)

    # Phase 7: layer 3-day metrics + industry context on top of the bulk
    # quote dicts so each worker can write a fully-enriched row without
    # any extra network or DB queries inside the per-code phase.
    _enrich_with_three_day_and_industry(codes, bulk)

    lhb_map = collect_lhb_today() if post_close else {}

    # Now spawn workers — each handles per-code akshare + writes its own row.
    # Whole-phase ceiling protects against a worker that hangs past our
    # request-level timeout (rare, but observed when eastmoney slow-streams
    # bytes such that the per-call read timer keeps resetting). After the
    # ceiling we count unfinished futures as timed_out and return; stuck
    # threads keep running in the background until akshare itself gives up,
    # then exit harmlessly without writing (their session is closed).
    TOTAL_TIMEOUT = 240  # 4 min — covers normal day; bounds worst-case
    inserted = 0
    failed = 0
    timed_out = 0
    pool = ThreadPoolExecutor(max_workers=10)
    futures = {
        pool.submit(_snapshot_worker, c, bulk.get(c), lhb_map): c
        for c in codes
    }
    try:
        for fut in as_completed(futures, timeout=TOTAL_TIMEOUT):
            code = futures[fut]
            try:
                if fut.result():
                    inserted += 1
                else:
                    failed += 1
            except Exception:
                failed += 1
                logger.exception("snapshot worker for %s raised", code)
    except FuturesTimeout:
        # Anything still pending is the timed_out bucket.
        timed_out = sum(1 for f in futures if not f.done())
        for f, c in futures.items():
            if not f.done():
                logger.error("snapshot worker for %s exceeded %ds, abandoning",
                             c, TOTAL_TIMEOUT)
                f.cancel()  # best-effort; can't kill a thread mid-I/O
    pool.shutdown(wait=False)  # don't block return on stuck workers

    logger.info(
        "snapshot job: inserted %d/%d (failed=%d, timed_out=%d)",
        inserted, len(codes), failed, timed_out,
    )
    return {
        "codes": len(codes), "inserted": inserted,
        "failed": failed, "timed_out": timed_out, "post_close": post_close,
    }


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
    """Pull context fields (news/notices/lhb) AND prior values for any flaky
    field (main_net_flow + valuation) from the most recent snapshot.

    Quotes ticks usually don't refetch news/notices/lhb at all (heavy
    per-code fan-out). main_net_flow is flaky (sina doesn't carry it; the
    per-code akshare fund_flow fails intermittently on Railway). The
    valuation fields (pe/pb/换手率/市值) come from Tencent — when Tencent
    is down a sina-only tick would otherwise null them out. Carrying the
    previous value forward keeps the 盯盘 list visually stable instead of
    blinking to – every time one source hiccups.

    "prev_*" keys are extracted (popped) by the caller so they don't end
    up as Snapshot columns; the regular keys flow straight in.
    """
    if latest is None:
        return {
            "news": [], "notices": [], "lhb": None,
            "prev_main_net_flow": None,
            **{f"prev_{f}": None for f in VALUATION_FIELDS},
            **{f"prev_{f}": None for f in THREE_DAY_FIELDS},
            **{f"prev_{f}": None for f in INDUSTRY_FIELDS},
        }
    return {
        "news": latest.news or [],
        "notices": latest.notices or [],
        "lhb": latest.lhb,
        "prev_main_net_flow": latest.main_net_flow,
        **{f"prev_{f}": getattr(latest, f, None) for f in VALUATION_FIELDS},
        **{f"prev_{f}": getattr(latest, f, None) for f in THREE_DAY_FIELDS},
        **{f"prev_{f}": getattr(latest, f, None) for f in INDUSTRY_FIELDS},
    }


def run_quotes_job() -> dict:
    """Bulk-pull price + main flow for the whole watchlist in one shot.

    Writes a snapshot row per code, copying news/notices/lhb forward from
    the previous snapshot so signal detection and the detail view keep
    seeing the latest context fields without the heavy per-code fan-out.
    """
    ensure_extra_columns()  # self-heal; same reasoning as run_snapshot_job
    db: Session = SessionLocal()
    try:
        codes = [w.code for w in db.query(Watchlist.code).all()]
        if not codes:
            return {"codes": 0, "inserted": 0, "tier": "quotes"}

        logger.info("quotes job: bulk pulling %d codes", len(codes))
        bulk = collect_quotes_bulk(codes)
        # Phase 7: layer 3-day metrics + industry context (cheap, cached)
        # so each row written this tick carries the full set of fields,
        # not just price/flow.
        _enrich_with_three_day_and_industry(codes, bulk)

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
            # flicker when fund-flow is the flaky source. Same pattern for
            # the valuation fields — Tencent is reliable but not infallible.
            net_flow = quote.get("main_net_flow") or carry.pop("prev_main_net_flow")
            carry.pop("prev_main_net_flow", None)
            valuation = {
                f: (quote.get(f) if quote.get(f) is not None
                    else carry.pop(f"prev_{f}", None))
                for f in VALUATION_FIELDS
            }
            for f in VALUATION_FIELDS:
                carry.pop(f"prev_{f}", None)
            three_day_vals = {
                f: (quote.get(f) if quote.get(f) is not None
                    else carry.pop(f"prev_{f}", None))
                for f in THREE_DAY_FIELDS
            }
            for f in THREE_DAY_FIELDS:
                carry.pop(f"prev_{f}", None)
            industry_vals = {
                f: (quote.get(f) if quote.get(f) is not None
                    else carry.pop(f"prev_{f}", None))
                for f in INDUSTRY_FIELDS
            }
            for f in INDUSTRY_FIELDS:
                carry.pop(f"prev_{f}", None)
            snap = {
                "code": code,
                "price": quote.get("price"),
                "change_pct": quote.get("change_pct"),
                "volume": quote.get("volume"),
                "turnover": quote.get("turnover"),
                "main_net_flow": net_flow,
                "north_hold_change": None,
                **valuation,
                **three_day_vals,
                **industry_vals,
                **carry,
                # Phase 9: kline indicator dict (consumed by compute_signals,
                # not written to the Snapshot row). Falls through carry-forward
                # naturally since it's not in any *_FIELDS list.
                "kline": quote.get("kline"),
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
                **{f: snap[f] for f in VALUATION_FIELDS},
                **{f: snap[f] for f in THREE_DAY_FIELDS},
                **{f: snap[f] for f in INDUSTRY_FIELDS},
            )
            db.add(row)
            try:
                db.commit()
                inserted += 1
            except Exception:
                db.rollback()
                logger.exception("quotes job: failed to insert %s", code)
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


def run_daily_analysis_job(
    only_stale: bool = True,
    only_missing: bool = False,
    force: bool = False,
) -> dict:
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

    - `force=True` (6/3): skip ALL skip logic AND bypass the snapshot_id
      cache in generate(). Use for admin one-shot full repaint, e.g. after
      shipping a new schema field (valid_window) when you want every row
      to carry it immediately. Caller is on the hook for the cost.

    Pass `only_stale=False, only_missing=False, force=False` to honour the
    existing skip ladder but let generate() short-circuit on identical
    snapshot — i.e. "respect cache, just don't honour stale/missing".

    Runs serially. One bad code doesn't sink the rest. If ANTHROPIC_API_KEY
    isn't set we log and skip, so the job is harmless without a key.

    Codes pulled DISTINCT — multi-user watchlists overlap (heavy users have
    ~50 stocks, total/distinct ~ 142/100), and analyses are one-row-per-code
    globally shared, so running per-user would burn tokens for the same
    result. (Until 6/3 this query returned duplicates; SQL `.distinct()`
    fixes it.)
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.info("daily analysis job: ANTHROPIC_API_KEY not set, skipping")
        return {"codes": 0, "generated": 0, "failed": 0, "skipped": 0,
                "no_api_key": True}

    db: Session = SessionLocal()
    try:
        codes = [c for (c,) in db.query(Watchlist.code).distinct().all()]
        if not codes:
            logger.info("daily analysis job: watchlist empty, skipping")
            return {"codes": 0, "generated": 0, "failed": 0, "skipped": 0}

        logger.info(
            "daily analysis job: %d distinct codes, only_missing=%s only_stale=%s force=%s",
            len(codes), only_missing, only_stale, force,
        )
        generated = 0
        failed = 0
        skipped = 0
        for code in codes:
            # force=True: bypass all skip logic AND bypass cache in generate().
            if not force:
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
                analysis_generate(db, code, force=force)
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


def _kline_tick():
    """Daily 16:30 BJT post-close: refresh K-lines + indicators for the
    full watchlist union. Sequential ~1s/code; ~1min for 60 codes."""
    try:
        from . import kline as kline_svc
        kline_svc.pull_for_watchlist()
    except Exception:
        logger.exception("kline tick failed")


def _financials_tick():
    """Weekly: refresh financial statements for the full watchlist. Sina
    endpoint is per-stock ~1s, parallelized 8-way internally — ~90s for
    60 codes. Earnings-window dates (10-15 of Apr/Aug/Oct ish) refresh
    more often than weekly is value but we keep weekly as the default."""
    try:
        from . import financials as fin_svc
        fin_svc.pull_for_watchlist()
    except Exception:
        logger.exception("financials tick failed")


# ---------------------------------------------------------------------------
# Smart intraday analysis — every 30 min during trading hours, only repaint
# codes whose snapshot meaningfully changed since their last analysis.
# Addresses the heavy-user complaint that 分钟级 snapshot 数据没被自动
# LLM 消费 ("拿到了数据不消费,数据价值就弱了"). Distinct codes ≈ 100
# in prod (5/29 watchlist-stats), so全量 cap ~5 元/cycle. Increment filter
# usually keeps us to ~5-15 codes per cycle = 0.25-0.75 元.
#
# The 触发条件 logic itself lives in analysis.should_reanalyze() so the
# list view's is_fresh badge can share it — when smart cron decides
# "this row needs repaint", we want the list to also say "已过期".
# ---------------------------------------------------------------------------

# Module-level last-run state. Same pattern as refresh-financials /
# backfill-outcomes async endpoints — diag endpoint reads this.
_smart_state: dict = {
    "running": False,
    "last_result": None,   # dict from last run, populated on completion
    "last_started_at": None,  # ISO ts
}


def run_smart_intraday_analysis() -> dict:
    """Distinct-code 增量 batch. Scans every unique watchlist code,
    applies _should_reanalyze, calls analysis.generate(force=False) for
    those that pass — force=False lets the snapshot_id cache short-
    circuit any false positives that slipped through (defense in depth).

    Returns counters by reason tag for the diag endpoint.
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.info("smart intraday analysis: ANTHROPIC_API_KEY not set, skipping")
        return {"skipped_no_key": True}
    if _smart_state["running"]:
        logger.info("smart intraday analysis: already running, skipping")
        return {"skipped_already_running": True}

    _smart_state["running"] = True
    _smart_state["last_started_at"] = datetime.now(timezone.utc).isoformat()
    db: Session = SessionLocal()
    try:
        codes = [c for (c,) in db.query(Watchlist.code).distinct().all()]
        if not codes:
            result = {"distinct_codes": 0, "triggered": 0, "by_reason": {}}
            _smart_state["last_result"] = result
            return result

        # Bulk-fetch latest snapshot per code (one query) + existing
        # analyses (one query) to avoid N+1.
        # Latest snapshot per code: subquery for max(id) per code, join.
        from sqlalchemy import func, and_
        latest_snap_subq = (
            db.query(Snapshot.code, func.max(Snapshot.id).label("max_id"))
            .filter(Snapshot.code.in_(codes))
            .group_by(Snapshot.code)
            .subquery()
        )
        latest_snaps = {
            s.code: s for s in db.query(Snapshot)
            .join(latest_snap_subq, and_(
                Snapshot.code == latest_snap_subq.c.code,
                Snapshot.id == latest_snap_subq.c.max_id,
            ))
            .all()
        }
        existing_rows = {
            a.code: a for a in db.query(Analysis)
            .filter(Analysis.code.in_(codes)).all()
        }
        # Anchor snapshots = snapshots referenced by existing analyses.
        anchor_ids = [a.snapshot_id for a in existing_rows.values()
                      if a.snapshot_id is not None]
        anchor_snaps = (
            {s.id: s for s in db.query(Snapshot)
             .filter(Snapshot.id.in_(anchor_ids)).all()}
            if anchor_ids else {}
        )

        by_reason: dict[str, int] = {}
        triggered: list[str] = []
        for code in codes:
            snap = latest_snaps.get(code)
            existing = existing_rows.get(code)
            anchor = (
                anchor_snaps.get(existing.snapshot_id)
                if (existing and existing.snapshot_id is not None) else None
            )
            should, tag = should_reanalyze(snap, existing, anchor,
                                           respect_cooldown=True)
            by_reason[tag] = by_reason.get(tag, 0) + 1
            if should:
                triggered.append(code)

        logger.info(
            "smart intraday analysis: distinct=%d triggered=%d by_reason=%s",
            len(codes), len(triggered), by_reason,
        )

        # Generate concurrently — kimi 实际 p77 延迟 > 60s,串行 60 个
        # stocks 跑 1 小时 (6/4 14:05 cycle 实测)。max_workers=5 让 cycle
        # 时间压到 1/5,配合 timeout=90 (留缓冲让多数 call 成功)。
        # 60 stocks × 90s / 5 = ~18 min,fit 30 min cycle 窗口。
        #
        # dashscope 并发限流未公开测过,5 算保守上限。如果撞限流,失败
        # stocks 进 failed,下个 cycle 自然重试,不影响整体稳定性。
        # 每个 worker 自己开 SessionLocal — analysis_generate 内有大量
        # DB ops,跨线程共享 session 会出 SQLAlchemy 错。
        SMART_MAX_WORKERS = 5
        generated = 0
        failed = 0
        cache_hit = 0

        # Pre-capture each triggered code's pre-call snapshot_id so the
        # worker can detect cache_hit without re-querying.
        before_ids: dict[str, int | None] = {
            code: (existing_rows[code].snapshot_id if existing_rows.get(code) else None)
            for code in triggered
        }

        def _generate_one(code: str) -> str:
            """Worker fn — returns one of 'generated' / 'cache_hit' / 'failed'."""
            worker_db: Session = SessionLocal()
            try:
                row = analysis_generate(worker_db, code, force=False)
                before = before_ids.get(code)
                if row.snapshot_id == before and before is not None:
                    return "cache_hit"
                return "generated"
            except Exception:
                logger.exception("smart intraday: %s failed", code)
                return "failed"
            finally:
                worker_db.close()

        if triggered:
            with ThreadPoolExecutor(
                max_workers=SMART_MAX_WORKERS,
                thread_name_prefix="smart-analyze",
            ) as pool:
                futures = [pool.submit(_generate_one, c) for c in triggered]
                for f in as_completed(futures):
                    status = f.result()
                    if status == "generated":
                        generated += 1
                    elif status == "cache_hit":
                        cache_hit += 1
                    else:
                        failed += 1

        result = {
            "distinct_codes": len(codes),
            "triggered": len(triggered),
            "generated": generated,
            "cache_hit": cache_hit,
            "failed": failed,
            "by_reason": by_reason,
            "triggered_codes": triggered[:30],  # sample for debugging
        }
        _smart_state["last_result"] = result
        return result
    finally:
        db.close()
        _smart_state["running"] = False


def _smart_analyze_tick():
    """APScheduler entry. Internal _is_intraday guard since cron can't
    express 'trading hours only' precisely (lunch break, holidays)."""
    # Local _is_intraday: A 股交易时段 + 工作日. Reuse the same logic as
    # data_completeness so behavior is consistent across the codebase.
    try:
        from .analysis import _is_intraday
        if not _is_intraday():
            return
        run_smart_intraday_analysis()
    except Exception:
        logger.exception("smart analyze tick failed")


def _outcomes_tick():
    """Daily post-close: fill forward returns on analysis outcomes. Runs
    after _kline_tick (16:30) so the latest close is already in the DB."""
    try:
        from . import outcomes as outcomes_svc
        outcomes_svc.backfill_outcomes()
    except Exception:
        logger.exception("outcomes tick failed")


def _shareholder_tick():
    """Daily post-close 17:30 BJT: pull market-wide insider shareholding
    changes (董监高 / 高管 / 配偶子女增减持), filter to watchlist + 90 days,
    upsert into shareholder_changes. ~30s wall-time (akshare single bulk
    call). Used by analysis.py prompt to surface 内部人交易 signal."""
    try:
        from . import shareholder as shareholder_svc
        shareholder_svc.pull_for_watchlist()
    except Exception:
        logger.exception("shareholder tick failed")


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
    # Phase 9: K-line + indicators refresh, daily 16:30 BJT (post-close).
    # ~1s/code so 60 codes in ~1min. Falls outside the snapshot/quotes
    # paths so it doesn't compete with them.
    sched.add_job(
        _kline_tick,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone="Asia/Shanghai"),
        id="kline_16_30",
        replace_existing=True,
        misfire_grace_time=1800,
    )
    # Phase 10: financial-statement refresh — weekly Monday 08:00 BJT
    # before the morning analysis pass. Earnings windows (~10-15 Apr/Aug/Oct)
    # may benefit from daily refresh; if we feel the lag we can flip
    # day_of_week to mon-fri later.
    sched.add_job(
        _financials_tick,
        CronTrigger(day_of_week="mon", hour=8, minute=0, timezone="Asia/Shanghai"),
        id="financials_mon_08_00",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 6/3: Smart intraday analysis — every 30 min during trading hours,
    # only repaint codes whose snapshot meaningfully changed. Cron casts
    # a slightly wider net (09-14 mon-fri, every :05 and :35) so the
    # tick fires near top-of-hour and bottom-of-hour just after the
    # 5-min quotes job; _smart_analyze_tick() then internal-guards with
    # _is_intraday() to skip lunch break + non-trading days.
    # First job fires at 09:35 — after 09:30 open quotes + after the
    # 09:35 daily_analysis_tick which bootstrap-fills cold codes.
    sched.add_job(
        _smart_analyze_tick,
        CronTrigger(
            day_of_week="mon-fri", hour="9-14", minute="5,35",
            timezone="Asia/Shanghai",
        ),
        id="smart_analyze_30min",
        replace_existing=True,
        misfire_grace_time=600,
    )
    # Analysis-outcome backfill — daily 17:00 BJT, after the 16:30 kline
    # tick so the latest close is available.
    sched.add_job(
        _outcomes_tick,
        CronTrigger(day_of_week="mon-fri", hour=17, minute=0, timezone="Asia/Shanghai"),
        id="outcomes_17_00",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # Shareholder change refresh — daily 17:30 BJT (after _outcomes_tick).
    # 30s wall-time akshare bulk fetch; insider trading events are post-
    # close anyway,所以盘后跑最合适。
    sched.add_job(
        _shareholder_tick,
        CronTrigger(day_of_week="mon-fri", hour=17, minute=30, timezone="Asia/Shanghai"),
        id="shareholder_17_30",
        replace_existing=True,
        misfire_grace_time=3600,
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
