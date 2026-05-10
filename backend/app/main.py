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
    Analysis, IndustryMeta, InviteCode, Kline, SectorPicks, Snapshot, User, Watchlist,
)
from .routes import auth as auth_routes
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


@app.post("/api/_diag/refresh-industry-meta")
def diag_refresh_industry_meta():
    """One-shot industry mapping pull. Phase 7 stores per-stock 行业 in
    the industry_meta table; without this, the snapshot job has no map
    to compute industry percentiles + averages from. Call once after
    deploy (or after adding new stocks)."""
    from .services import industry as industry_svc
    return industry_svc.refresh_industry_meta()


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


@app.get("/health")
def health():
    return {"status": "ok"}
