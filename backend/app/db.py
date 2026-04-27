import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
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
]


def ensure_extra_columns() -> None:
    """Add post-MVP columns to existing tables when missing.

    create_all only creates *missing tables*, never alters columns of an
    existing one — so when the model gains fields after a deploy, prod
    Postgres doesn't pick them up. This helper closes that gap on startup.
    """
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        for table, col, dtype in _POSTGRES_BACKFILL:
            try:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {dtype}"
                ))
            except Exception as e:
                logger.warning("ensure_extra_columns: %s.%s failed: %s", table, col, e)
