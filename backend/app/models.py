from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Index, Integer, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Watchlist(Base):
    __tablename__ = "watchlist"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    exchange: Mapped[str] = mapped_column(String(2), nullable=False)  # sh / sz / bj
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Snapshot(Base):
    """One row per (code, scrape time). Hourly during A-share trading hours."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Core market data
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)  # 成交量(股)
    turnover: Mapped[float | None] = mapped_column(Float, nullable=True)  # 成交额(元)

    # Fund/北向 flow
    main_net_flow: Mapped[float | None] = mapped_column(Float, nullable=True)  # 主力净流入(元)
    north_hold_change: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Signals: list[str] of signal codes (e.g. ["limit_up", "lhb"])
    signals: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    # Latest news/notices snapshot — list of {title, url, ts, type?}
    news: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    notices: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)

    # LHB info (only populated on the post-close 16:00 tick when stock is on LHB)
    lhb: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_snapshots_code_ts", "code", "ts"),
        Index("ix_snapshots_ts", "ts"),
    )


class Analysis(Base):
    """Cached LLM-generated analysis for a stock. One row per code (latest only)."""

    __tablename__ = "analyses"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    key_table: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    deep_analysis: Mapped[str] = mapped_column(String, nullable=False)  # markdown
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str] = mapped_column(String(50), nullable=False)
    strategy: Mapped[str] = mapped_column(String(50), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
