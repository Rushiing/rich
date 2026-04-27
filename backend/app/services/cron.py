"""APScheduler wiring + the snapshot job.

A-share trading hours: 09:30-11:30, 13:00-15:00 (Asia/Shanghai).
We snapshot at 09:30, 10:30, 11:30, 14:00, 15:00 + 16:00 (post-close LHB pass).
We also auto-generate LLM analyses once per trading morning at 09:35,
right after the open snapshot, so the 盯盘 list has fresh verdicts on
arrival without anyone needing to click 解析.
The scheduler runs in-process; Railway must be pinned to 1 backend replica.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import Snapshot, Watchlist
from .analysis import generate as analysis_generate
from .scraper import collect_lhb_today, collect_many
from .signals import compute_signals

logger = logging.getLogger(__name__)

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


def run_daily_analysis_job() -> dict:
    """Generate (and cache) a fresh LLM analysis for every watched code.

    Runs serially — each call is one Anthropic round-trip — so the whole
    batch for ~30 codes takes a couple of minutes. One bad code doesn't
    sink the rest. If ANTHROPIC_API_KEY is unset we log and skip, so this
    job is harmless when the key isn't configured yet.
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.info("daily analysis job: ANTHROPIC_API_KEY not set, skipping")
        return {"codes": 0, "generated": 0, "failed": 0, "skipped": True}

    db: Session = SessionLocal()
    try:
        codes = [w.code for w in db.query(Watchlist.code).all()]
        if not codes:
            logger.info("daily analysis job: watchlist empty, skipping")
            return {"codes": 0, "generated": 0, "failed": 0}

        logger.info("daily analysis job: generating for %d codes", len(codes))
        generated = 0
        failed = 0
        for code in codes:
            try:
                analysis_generate(db, code)
                generated += 1
            except Exception:
                failed += 1
                logger.exception("daily analysis job: %s failed", code)
        logger.info("daily analysis job: generated=%d failed=%d", generated, failed)
        return {"codes": len(codes), "generated": generated, "failed": failed}
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
    sched.start()
    scheduler = sched
    logger.info("scheduler started: %d snapshot jobs + 1 analysis job", len(CRON_TIMES))


def stop_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None
