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
    # Phase 6.5: password auth — existing rows stay NULL until the admin
    # reset script populates them.
    ("users",     "password_hash",            "VARCHAR(128)"),
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


def migrate_watchlist_pk() -> None:
    """Switch watchlist PK from `code` to synthetic `id` BIGSERIAL, with
    UNIQUE(user_id, code) so the SAME user can't double-add a code but
    different users can each own the same code.

    Idempotent — checks current state first. Postgres-only (SQLite tests
    start from fresh schema with the new model and don't need this).
    Runs in lifespan AFTER ensure_extra_columns (which adds user_id) and
    BEFORE migrate_admin_watchlist (which fills user_id).
    """
    if engine.dialect.name != "postgresql":
        return
    try:
        with engine.begin() as conn:
            # Already migrated? Check for the `id` column.
            has_id = conn.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'watchlist' AND column_name = 'id'"
            )).first() is not None
            if has_id:
                return
            # Drop existing PK on `code` (name might be watchlist_pkey or
            # autogenerated; query pg_constraint for safety).
            pk_name = conn.execute(text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'watchlist'::regclass AND contype = 'p'"
            )).scalar()
            if pk_name:
                conn.execute(text(f'ALTER TABLE watchlist DROP CONSTRAINT "{pk_name}"'))
                logger.info("migrate_watchlist_pk: dropped old PK %s", pk_name)
            # Add synthetic id BIGSERIAL PK.
            conn.execute(text(
                "ALTER TABLE watchlist ADD COLUMN id BIGSERIAL PRIMARY KEY"
            ))
            logger.info("migrate_watchlist_pk: added id BIGSERIAL PK")
            # Add unique constraint (user_id, code). Existing rows are all
            # admin-owned via migrate_admin_watchlist by the time a SECOND
            # user tries to add — but at the moment of running this DDL we
            # may have rows where user_id is still NULL (unmigrated). UNIQUE
            # treats NULLs as distinct so this is safe to add now.
            conn.execute(text(
                "ALTER TABLE watchlist ADD CONSTRAINT uq_watchlist_user_code "
                "UNIQUE (user_id, code)"
            ))
            logger.info("migrate_watchlist_pk: added uq_watchlist_user_code")
    except Exception as e:
        logger.error("migrate_watchlist_pk FAILED: %s", e)


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
