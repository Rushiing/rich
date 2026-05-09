import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

# Pool sizing: snapshot_job runs 10 worker threads, each grabbing a DB
# session at write time. Add room for frontend polling (~3 concurrent),
# scheduler, lifespan, and analysis worker. 20 base + 20 overflow comfortably
# absorbs a snapshot batch without anyone parking on connection acquisition.
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=20,
    pool_recycle=1800,  # recycle every 30min to dodge silent stale connections
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Columns added to existing tables after the initial create_all. Each entry
# is (table, column_name, sql_type). Postgres uses ADD COLUMN IF NOT EXISTS;
# SQLite is safe because smoke tests start from fresh DBs (create_all gives
# them every column from the model).
_POSTGRES_BACKFILL = [
    ("snapshots", "pe_ratio",         "DOUBLE PRECISION"),
    ("snapshots", "pb_ratio",         "DOUBLE PRECISION"),
    ("snapshots", "turnover_rate",    "DOUBLE PRECISION"),
    ("snapshots", "market_cap",       "DOUBLE PRECISION"),
    ("snapshots", "circ_market_cap",  "DOUBLE PRECISION"),
    ("watchlist", "starred",          "BOOLEAN NOT NULL DEFAULT FALSE"),
    # Phase 6: user system. Nullable on rollout; admin migration backfills
    # NULL rows to ADMIN_PHONE's user.id on the next lifespan startup.
    ("watchlist", "user_id",          "INTEGER"),
    # Phase 7: 3-day rolling metrics + industry context on each snapshot.
    ("snapshots", "change_pct_3d",            "DOUBLE PRECISION"),
    ("snapshots", "turnover_rate_3d",         "DOUBLE PRECISION"),
    ("snapshots", "net_flow_3d",              "DOUBLE PRECISION"),
    ("snapshots", "industry_name",            "VARCHAR(40)"),
    ("snapshots", "industry_pe_pctile",       "DOUBLE PRECISION"),
    ("snapshots", "industry_change_3d_pctile","DOUBLE PRECISION"),
    ("snapshots", "industry_flow_3d_pctile",  "DOUBLE PRECISION"),
    ("snapshots", "industry_pe_avg",          "DOUBLE PRECISION"),
    ("snapshots", "industry_pb_avg",          "DOUBLE PRECISION"),
]


def ensure_extra_columns() -> None:
    """Add post-MVP columns to existing tables when missing.

    create_all only creates *missing tables*, never alters columns of an
    existing one — so when the model gains fields after a deploy, prod
    Postgres doesn't pick them up. This helper closes that gap.

    Logs each column individually at INFO so we can confirm in production
    logs whether the migration actually ran. Uses a fresh transaction per
    column so one failure can't poison the others.
    """
    if engine.dialect.name != "postgresql":
        return
    added = 0
    skipped = 0
    failed = 0
    for table, col, dtype in _POSTGRES_BACKFILL:
        try:
            with engine.begin() as conn:
                # Detect first so we can log "added" vs "already there".
                exists = conn.execute(text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ), {"t": table, "c": col}).first() is not None
                if exists:
                    skipped += 1
                    continue
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}"))
                logger.info("ensure_extra_columns: added %s.%s (%s)", table, col, dtype)
                added += 1
        except Exception as e:
            failed += 1
            logger.error("ensure_extra_columns: %s.%s FAILED: %s", table, col, e)
    logger.info(
        "ensure_extra_columns: done — added=%d, already_there=%d, failed=%d",
        added, skipped, failed,
    )


def snapshot_columns() -> list[str]:
    """Return the actual column names of the snapshots table from pg_catalog.

    Used by the /api/_diag/snapshot-schema route so we can verify from outside
    whether the migration ran without needing Railway shell access.
    """
    if engine.dialect.name != "postgresql":
        # SQLite (smoke tests): pull from PRAGMA
        with engine.begin() as conn:
            rows = conn.execute(text("PRAGMA table_info(snapshots)")).fetchall()
        return [r[1] for r in rows]
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'snapshots' ORDER BY ordinal_position"
        )).fetchall()
    return [r[0] for r in rows]
