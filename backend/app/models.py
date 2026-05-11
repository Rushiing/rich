from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON,
    String, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class User(Base):
    """SMS-verified end user.

    `phone` is the canonical identity (11-digit Chinese mobile). We don't
    store passwords — every login goes through SMS verification, and
    persistence relies on the signed cookie carrying user_id (see
    auth.issue_token / verify_token). Phone is unique; a new login on the
    same number just re-signs a new cookie for the existing row.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(11), unique=True, nullable=False, index=True)
    phone_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Phase 6.5: password-based auth replacing dev-mode SMS. Nullable on
    # rollout — existing SMS-verified rows have NULL until the migration
    # script (or admin endpoint) sets a temporary hash. Users with a NULL
    # password_hash can't log in via /auth/login; they need an admin reset
    # or fall back to the legacy SMS dev path until that's removed.
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)


class InviteCode(Base):
    """Invite codes for self-service registration.

    Two modes:
      - one-shot (max_uses=1, default): a single registration consumes it
      - shared (max_uses=NULL or N>1): unlimited or N reuses, e.g. one
        general code that gets shared in a group chat

    `current_uses` counts how many times the code has been redeemed;
    `used_at` / `used_by_user_id` record the FIRST consumer (kept for
    audit, repurposed for backwards compat with the one-shot rows
    created before this column existed).
    """
    __tablename__ = "invite_codes"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # First-use audit. NULL until the code is first redeemed.
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    used_by_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    # NULL = unlimited reuse, integer N = up to N redemptions.
    # Default 1 preserves the original one-shot behavior.
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    current_uses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    # Optional human-readable label so the issuer remembers who they
    # handed the code to (e.g. "for 老王" or "通用邀请码 v1").
    note: Mapped[str | None] = mapped_column(String(60), nullable=True)


class Watchlist(Base):
    __tablename__ = "watchlist"

    # PK choice: synthetic `id` so different users can each own the same
    # code (Phase 6 multi-user). The original schema had code-as-PK back
    # when there was one shared watchlist; we migrate that in
    # `db.migrate_watchlist_pk` (idempotent) — drops the old PK, adds a
    # BIGSERIAL id, and adds UNIQUE(user_id, code) to prevent the SAME
    # user double-adding a code.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    exchange: Mapped[str] = mapped_column(String(2), nullable=False)  # sh / sz / bj
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # User-marked "特别关注" — pinned to the top of its actionable bucket
    # in the 盯盘 list. Default False so existing rows don't suddenly all
    # float up on rollout.
    starred: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "code", name="uq_watchlist_user_code"),
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

    # Valuation + activity (sourced from Tencent qt.gtimg.cn). Optional —
    # nullable for any tick where Tencent didn't cover the code or wasn't
    # reachable. Used to thicken the LLM prompt for the analysis pipeline.
    pe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)  # 换手率 (%)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)  # 总市值 (元)
    circ_market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)  # 流通市值 (元)

    # Phase 7: 3-day rolling metrics. Pulled from akshare's
    # stock_fund_flow_individual('3日排行') (THS via Tencent) when the code
    # ranks; for codes outside the rank list we aggregate from our own
    # snapshot history (services/aggregates.py). Both paths are best-effort
    # and may leave any of these None.
    change_pct_3d: Mapped[float | None] = mapped_column(Float, nullable=True)    # 3日涨幅 (%)
    turnover_rate_3d: Mapped[float | None] = mapped_column(Float, nullable=True) # 3日换手率 累计 (%)
    net_flow_3d: Mapped[float | None] = mapped_column(Float, nullable=True)      # 3日主力净流入 (元)

    # Phase 7: industry context. industry_name is the canonical Sina/CNINFO
    # name (e.g., "酿酒行业"); the *_pctile fields are 0-100 percentiles of
    # this code WITHIN its industry across the latest snapshot pool. _avg
    # fields are industry-level averages, exposed for the LLM prompt.
    industry_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    industry_pe_pctile: Mapped[float | None] = mapped_column(Float, nullable=True)
    industry_change_3d_pctile: Mapped[float | None] = mapped_column(Float, nullable=True)
    industry_flow_3d_pctile: Mapped[float | None] = mapped_column(Float, nullable=True)
    industry_pe_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    industry_pb_avg: Mapped[float | None] = mapped_column(Float, nullable=True)

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


class Kline(Base):
    """Daily K-line + computed technical indicators for one (code, date).

    Phase 9: 60-day rolling cache. Refreshed by `_kline_tick` post-close
    (16:30) once per trading day, idempotent UPSERT keyed by (code, date).
    Indicators computed with hand-rolled formulas (no pandas-ta on
    Railway's Python wheels) — see services/kline.py.
    """
    __tablename__ = "klines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), nullable=False)
    date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD

    open: Mapped[float | None] = mapped_column(Float, nullable=True)
    close: Mapped[float | None] = mapped_column(Float, nullable=True)
    high: Mapped[float | None] = mapped_column(Float, nullable=True)
    low: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    # MAs
    ma5: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma20: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma60: Mapped[float | None] = mapped_column(Float, nullable=True)

    # MACD (12, 26, 9)
    macd_dif: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_dea: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_hist: Mapped[float | None] = mapped_column(Float, nullable=True)

    # BOLL (20, 2)
    boll_mid: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_up: Mapped[float | None] = mapped_column(Float, nullable=True)
    boll_low: Mapped[float | None] = mapped_column(Float, nullable=True)

    # KDJ (9, 3, 3)
    kdj_k: Mapped[float | None] = mapped_column(Float, nullable=True)
    kdj_d: Mapped[float | None] = mapped_column(Float, nullable=True)
    kdj_j: Mapped[float | None] = mapped_column(Float, nullable=True)

    # RSI
    rsi6: Mapped[float | None] = mapped_column(Float, nullable=True)
    rsi12: Mapped[float | None] = mapped_column(Float, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("code", "date", name="uq_klines_code_date"),
        Index("ix_klines_code_date", "code", "date"),
    )


class IndustryMeta(Base):
    """Stock-code → industry mapping. Refreshed weekly from
    akshare.stock_industry_category_cninfo() (CNINFO taxonomy). One row per
    code; industry name is the canonical Sina/CNINFO name we then group by
    when computing per-industry percentiles + averages."""
    __tablename__ = "industry_meta"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    industry_name: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    industry_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class Financial(Base):
    """One row per (code, report_date) — financial highlights pulled from
    akshare's `stock_financial_abstract` (sina).

    We extract ~10 key indicators rather than the full 80-row wide table
    sina ships. The composite PK (code, report_date) makes idempotent
    upserts straightforward.

    Refreshed weekly + on demand via /api/_diag/refresh-financials. During
    earnings windows (April / August / October) consider daily refresh.
    """
    __tablename__ = "financials"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    report_date: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYYMMDD
    # 营业总收入 (元)
    total_revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 归母净利润 (元)
    net_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 扣非净利润 (元)
    net_profit_excl_nr: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 毛利率 (%)
    gross_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 销售净利率 (%)
    net_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ROE (%)
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 营业总收入增长率 (%) — 同比
    revenue_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 归属母公司净利润增长率 (%) — 同比
    profit_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 资产负债率 (%)
    debt_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 期间费用率 (%) — for the 三费占比 > 50% red flag rule
    expense_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_financials_code_date", "code", "report_date"),
    )


class SectorPicks(Base):
    """LLM-curated daily sector recommendations.

    One row only — id is fixed at 1 and we replace the row each refresh.
    `payload` carries the full structured pick result (top N sectors × K
    picks each, with per-sector and per-stock reasons). TTL check happens
    in the route based on `generated_at`.
    """
    __tablename__ = "sector_picks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model: Mapped[str] = mapped_column(String(50), nullable=False)


class Analysis(Base):
    """Cached LLM-generated analysis for a stock. One row per code (latest only)."""

    __tablename__ = "analyses"

    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    key_table: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    deep_analysis: Mapped[str] = mapped_column(String, nullable=False)  # markdown
    snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str] = mapped_column(String(50), nullable=False)
    strategy: Mapped[str] = mapped_column(String(50), nullable=False)
    # Prompt version identifier — bump in services/analysis.py whenever the
    # tool schema or system prompt changes. Lets us correlate quality
    # differences across versions when the hit-rate tracker (ROADMAP P1)
    # comes online.
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Generation mode: "single" (default, one LLM call) vs "debate" (three-
    # role bull/bear/judge pipeline). Persisted so the frontend can show
    # whether this result came from the deeper path and adjust UX (banner,
    # auto-scroll to 看多 vs 看空 section, etc.)
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True, default="single")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
