import logging
import os
from contextlib import asynccontextmanager

# Silence akshare's per-call tqdm progress bars BEFORE akshare gets imported.
# Without this, every `stock_news_em` / `stock_notice_report` / `stock_lhb_*`
# call writes a multi-line progress bar to stderr; uvicorn tags everything on
# stderr as `[err]`, so logs look like a fire even when the job is healthy.
# Both the env var and the monkey-patch are belt-and-suspenders — different
# tqdm versions honor different things, akshare uses its own bundled tqdm in
# some places.
os.environ.setdefault("TQDM_DISABLE", "1")
try:
    from functools import partialmethod
    import tqdm as _tqdm
    _tqdm.tqdm.__init__ = partialmethod(_tqdm.tqdm.__init__, disable=True)
except Exception:  # pragma: no cover — defensive
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import Base, engine, ensure_extra_columns, migrate_watchlist_pk, snapshot_columns
from .models import (  # noqa: F401  (register tables with metadata)
    Analysis, AnalysisOutcome, Financial, Holding, IndustryMeta, InviteCode,
    Kline, SectorPicks, Snapshot, User, Watchlist,
)
from .routes import auth as auth_routes
from .routes import holdings as holdings_routes
from .routes import market as market_routes
from .routes import sectors as sectors_routes
from .routes import stocks as stocks_routes
from .routes import watchlist as watchlist_routes
from .services.cron import start_scheduler, stop_scheduler
from .services.users import migrate_admin_watchlist

# Default Python logging swallows INFO; that hid `snapshot job: ...` and
# `scheduler started: ...` from Railway logs and made cron health hard to
# diagnose. Promote our own loggers to INFO without touching uvicorn's.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # MVP: create tables on startup. Switch to Alembic when we need column changes.
    Base.metadata.create_all(bind=engine)
    # create_all doesn't add columns to existing tables; close that gap.
    ensure_extra_columns()
    # Phase 6 fix: switch watchlist PK from `code` to synthetic `id` so
    # different users can own the same code. Must run BEFORE the admin
    # backfill (which inserts/updates rows) and AFTER ensure_extra_columns
    # (which adds the user_id column).
    migrate_watchlist_pk()
    # Phase 6: backfill watchlist.user_id for existing rows (idempotent).
    # Skipped silently when ADMIN_PHONE is empty.
    migrate_admin_watchlist()
    if settings.SCHEDULER_ENABLED:
        start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="rich backend", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.FRONTEND_ORIGIN.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router)
app.include_router(watchlist_routes.router)
app.include_router(stocks_routes.router)
app.include_router(sectors_routes.router)
app.include_router(holdings_routes.router)
app.include_router(market_routes.router)


@app.post("/api/_diag/refresh-industry-meta")
def diag_refresh_industry_meta():
    """One-shot industry mapping pull. Phase 7 stores per-stock 行业 in
    the industry_meta table; without this, the snapshot job has no map
    to compute industry percentiles + averages from. Call once after
    deploy (or after adding new stocks)."""
    from .services import industry as industry_svc
    return industry_svc.refresh_industry_meta()


_financials_lock = __import__("threading").Lock()
_financials_running = {"v": False, "last_result": None}


@app.post("/api/_diag/refresh-financials")
def diag_refresh_financials():
    """One-shot financials bootstrap, ASYNC. Pulls 8 quarters per code in
    the watchlist via akshare's stock_financial_abstract (sina). ~90s for
    a 60-code watchlist — Railway's HTTP proxy kills synchronous requests
    around 30s, so we run in a background thread and surface progress
    via /api/_diag/refresh-financials/status.

    Safe to re-run, upsert by (code, report_date). Already-running calls
    return {already_running: true} without firing a second job."""
    import threading
    from .services import financials as fin_svc

    with _financials_lock:
        if _financials_running["v"]:
            return {"started": False, "already_running": True}
        _financials_running["v"] = True
        _financials_running["last_result"] = None

    def _worker():
        try:
            result = fin_svc.pull_for_watchlist()
            _financials_running["last_result"] = result
        except Exception as e:
            _financials_running["last_result"] = {"error": str(e)}
        finally:
            with _financials_lock:
                _financials_running["v"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True}


@app.get("/api/_diag/refresh-financials/status")
def diag_refresh_financials_status():
    """Status of the most recent /refresh-financials run.

    Returns {running, progress, last_result}:
      - running: True while the worker is in flight
      - progress: live counters {done, ok, failed, total, current} so the
        client can show "5/61 done, currently fetching 600519" instead of
        a black-box spinner
      - last_result: None until the worker completes; afterwards holds
        the final counters dict or {error: msg}
    """
    from .services.financials import get_progress
    return {
        "running": _financials_running["v"],
        "progress": get_progress(),
        "last_result": _financials_running["last_result"],
    }


_outcomes_backfill_lock = __import__("threading").Lock()
_outcomes_backfill_running = {"v": False, "last_result": None}


@app.post("/api/_diag/backfill-outcomes")
def diag_backfill_outcomes():
    """Manually run the analysis-outcome backfill, ASYNC. Backfill walks
    the entire outcomes table (1k+ rows in prod) and runs a kline query
    per row — easily 60+ seconds, which Railway's HTTP proxy kills. So
    we fire a background thread and surface progress via the status
    endpoint, same shape as refresh-financials. Normal cadence is
    daily 17:00 BJT via _outcomes_tick; this endpoint is for ad-hoc
    catch-up runs after a long backend outage or schema change."""
    import threading
    from .services import outcomes as outcomes_svc

    with _outcomes_backfill_lock:
        if _outcomes_backfill_running["v"]:
            return {"started": False, "already_running": True}
        _outcomes_backfill_running["v"] = True
        _outcomes_backfill_running["last_result"] = None

    def _worker():
        try:
            result = outcomes_svc.backfill_outcomes()
            _outcomes_backfill_running["last_result"] = result
        except Exception as e:
            _outcomes_backfill_running["last_result"] = {"error": str(e)}
        finally:
            with _outcomes_backfill_lock:
                _outcomes_backfill_running["v"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True}


@app.get("/api/_diag/backfill-outcomes/status")
def diag_backfill_outcomes_status():
    """Status of the most recent /backfill-outcomes run."""
    return {
        "running": _outcomes_backfill_running["v"],
        "last_result": _outcomes_backfill_running["last_result"],
    }


@app.get("/api/_diag/klines-status")
def diag_klines_status():
    """Quick health check on the klines table — backfill_outcomes depends
    on it, so when scored_d5 stays at zero this is the first thing to
    inspect. Returns per-code coverage stats."""
    from sqlalchemy import func as sa_func
    from .db import SessionLocal
    from .models import Kline
    db = SessionLocal()
    try:
        total = db.query(sa_func.count(Kline.id)).scalar() or 0
        distinct_codes = db.query(
            sa_func.count(sa_func.distinct(Kline.code))
        ).scalar() or 0
        first = db.query(sa_func.min(Kline.date)).scalar()
        last = db.query(sa_func.max(Kline.date)).scalar()
        # Per-code row count distribution (min / median-ish / max)
        per_code = sorted([
            n for (code, n) in db.query(
                Kline.code, sa_func.count(Kline.id),
            ).group_by(Kline.code).all()
        ])
        median_per_code = per_code[len(per_code) // 2] if per_code else 0
        return {
            "total_rows": total,
            "distinct_codes": distinct_codes,
            "first_date": str(first) if first else None,
            "last_date": str(last) if last else None,
            "rows_per_code_min": per_code[0] if per_code else 0,
            "rows_per_code_median": median_per_code,
            "rows_per_code_max": per_code[-1] if per_code else 0,
            # If max != median, some codes are missing days — likely the
            # ones added to watchlist most recently. Backfill_outcomes
            # for those codes' anchors will keep returning "not enough
            # future bars yet" until kline_tick catches up.
        }
    finally:
        db.close()


@app.get("/api/_diag/outcomes-stats")
def diag_outcomes_stats():
    """Hit-rate summary grouped by prompt_version + actionable verdict.
    A 'hit' = 建议买入 with return_d5 > 0, or 建议卖出 with return_d5 < 0.
    Public on purpose — no secrets, lets us watch quality without shell
    access."""
    from .services import outcomes as outcomes_svc
    return outcomes_svc.hit_rate_stats()


@app.post("/api/_diag/refresh-klines")
def diag_refresh_klines():
    """One-shot K-line bootstrap. Phase 9's _kline_tick fires at 16:30 BJT
    only — if the user wants technical面 data before the first scheduled
    tick (e.g., this Sunday before Monday open), POST here. Synchronous;
    ~1 minute for a 60-code watchlist."""
    from .services import kline as kline_svc
    return kline_svc.pull_for_watchlist()


@app.get("/api/_diag/snapshot-schema")
def diag_snapshot_schema():
    """Surface the actual snapshots table column list. Public on purpose —
    no secrets in column names, and AUTH_DISABLED is on anyway. Lets us
    verify migrations from outside without Railway shell access.

    Expected columns after the 4/27 schema bump: id, code, ts, price,
    change_pct, volume, turnover, main_net_flow, north_hold_change,
    signals, news, notices, lhb, pe_ratio, pb_ratio, turnover_rate,
    market_cap, circ_market_cap.
    """
    cols = snapshot_columns()
    expected_extras = ["pe_ratio", "pb_ratio", "turnover_rate", "market_cap", "circ_market_cap"]
    missing = [c for c in expected_extras if c not in cols]
    return {
        "dialect": engine.dialect.name,
        "columns": cols,
        "expected_extras": expected_extras,
        "missing_extras": missing,
        "ok": len(missing) == 0,
    }


@app.get("/api/_diag/outcomes-detail")
def diag_outcomes_detail():
    """Raw distribution of the analysis_outcomes table — diagnoses why
    hit_rate_stats is sparse. Breaks down total / scorable / actually-
    scored counts by actionable, plus distinct modes/prompt_versions and
    the time window of recorded anchors.

    Hit-rate-stats alone hides the fact that "scored" (return_d5 not null)
    might be 4% of total because either record_anchor is silently skipping
    most calls, or the daily backfill cron isn't keeping up. This endpoint
    shows the two layers separately."""
    from sqlalchemy import func as sa_func
    from .db import SessionLocal
    from .models import AnalysisOutcome
    db = SessionLocal()
    try:
        total = db.query(sa_func.count(AnalysisOutcome.id)).scalar() or 0
        scored = db.query(sa_func.count(AnalysisOutcome.id)).filter(
            AnalysisOutcome.return_d5.isnot(None)
        ).scalar() or 0

        # Group by actionable (total vs scored)
        by_action: dict[str, dict] = {}
        for actionable, n in db.query(
            AnalysisOutcome.actionable, sa_func.count(AnalysisOutcome.id),
        ).group_by(AnalysisOutcome.actionable).all():
            by_action[actionable] = {"total": n, "scored": 0}
        for actionable, n in db.query(
            AnalysisOutcome.actionable, sa_func.count(AnalysisOutcome.id),
        ).filter(AnalysisOutcome.return_d5.isnot(None)).group_by(
            AnalysisOutcome.actionable,
        ).all():
            by_action.setdefault(actionable, {"total": 0, "scored": 0})
            by_action[actionable]["scored"] = n

        # Distinct mode / prompt_version
        modes = sorted({m for (m,) in db.query(AnalysisOutcome.mode).distinct().all() if m})
        prompts = sorted({p for (p,) in db.query(
            AnalysisOutcome.prompt_version,
        ).distinct().all() if p})

        # Time window
        first = db.query(sa_func.min(AnalysisOutcome.generated_at)).scalar()
        last = db.query(sa_func.max(AnalysisOutcome.generated_at)).scalar()

        # NULL anchor_price guard — record_anchor() skips when price is
        # None, so this should always be zero. If it isn't, the schema
        # has nullable=True (which it shouldn't given record_anchor's
        # behavior) and we have phantom rows.
        null_anchor = db.query(sa_func.count(AnalysisOutcome.id)).filter(
            AnalysisOutcome.anchor_price.is_(None)
        ).scalar() or 0

        return {
            "total_anchors": total,
            "scored_d5": scored,
            "scored_pct": round(scored / total * 100, 1) if total else None,
            "by_actionable": by_action,
            "distinct_modes": modes,
            "distinct_prompt_versions": prompts,
            "first_anchor": first.isoformat() if first else None,
            "last_anchor": last.isoformat() if last else None,
            "null_anchor_price_count": null_anchor,
        }
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}
