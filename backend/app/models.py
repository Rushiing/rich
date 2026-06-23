from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON,
    String, Text, UniqueConstraint, func,
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
    # 6/20: per-user 关注板块 — display-layer preference only. The pool
    # itself stays global (see virtual_pool.PRIORITY_SECTORS); this just
    # drives highlight/pin of matching entries on the /pool page. List of
    # theme strings matching PoolEntry.thesis.sector. NULL/[] = no prefs.
    preferred_sectors: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)


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
    # NOTE: deliberately no column `default` here. A scalar default fires
    # whenever the value is None at flush time — even an *explicit*
    # max_uses=None — so a column default would silently turn every
    # --unlimited code into a one-shot (max_uses=1). One-shot semantics
    # are handled in the caller (admin_users.py sets max_uses=1 explicitly
    # when neither --unlimited nor --max-uses is given).
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
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
    # 6/10: CNINFO 主营业务 — injected into the analysis prompt so the
    # LLM's 公司画像 stops depending on its (possibly stale or plain wrong)
    # world knowledge about what the company actually does. Same upstream
    # call as industry_name, no extra fetch cost.
    business_desc: Mapped[str | None] = mapped_column(Text, nullable=True)
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


class ShareholderChange(Base):
    """One row per insider (董监高 / 高管 / 配偶子女) shareholding change
    event. Pulled from akshare's `stock_hold_management_person_em` (东方
    财富 - 数据中心 - 特色数据 - 人员增减持明细).

    Used by the analysis prompt to surface "近 90 天大股东减持/增持 N 笔"
    signals — internal-person trading is one of the strongest single
    alpha indicators in 金融文献, and the LLM was working without it.

    Composite uniqueness: (code, change_date, person, change_shares).
    Same person can have multiple events on the same day (different
    trade reasons), so all four fields together identify a row.

    Refreshed daily 17:30 BJT via _shareholder_tick → market-wide pull
    then filter-to-watchlist. Retention: keep last 365 days (analysis
    only looks at last 90 but we keep more for future hit-rate joins).
    """
    __tablename__ = "shareholder_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), nullable=False)
    # 变动日期 (东财字段 "日期") — 实际成交日 / 变动日
    change_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    # 变动人姓名 (东财字段 "变动人") — 可能是本人,可能是配偶/子女
    person: Mapped[str] = mapped_column(String(40), nullable=False)
    # 变动股数 (东财字段 "变动股数") — 正负号通常体现增减,但有时是 abs;
    # 用 change_reason 判方向更稳。
    change_shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 成交均价 (元/股)
    avg_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 变动金额 (元)
    change_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 变动原因: 竞价交易 / 大宗交易 / 集合竞价 / 协议转让 / 二级市场买卖 / ...
    change_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 变动比例 (%) — 占总股本比例
    change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 变动后持股数 (股)
    holdings_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 董监高人员姓名 (有时跟 person 同,有时是 person 的"被关联董监高")
    insider_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 职务: 高管 / 董事 / 监事 / 总经理 / ...
    role: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 变动人与董监高的关系: 本人 / 配偶 / 子女 / 父母 / ...
    relation: Mapped[str | None] = mapped_column(String(20), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_shareholder_code_date", "code", "change_date"),
        # 防重 (code, date, person, shares) 一起做 dedupe key — 单纯
        # (code, date, person) 不够,同一人同一天可能多笔不同 reason 的
        # 交易 (如部分大宗 + 部分竞价)。
        Index(
            "uq_shareholder_dedupe",
            "code", "change_date", "person", "change_shares",
            unique=True,
        ),
    )


class AnalysisOutcome(Base):
    """Tracks how an analysis verdict played out over the following N
    trading days. One row per analysis *generation* (a regenerate creates
    a new row, so we accumulate a history).

    Anchor (code, generated_at, actionable, anchor_price) is written at
    generation time; the forward close_dN / return_dN columns are filled
    in by the daily _outcomes_tick cron as enough trading days elapse.

    Purpose: a feedback loop to measure whether prompt / pipeline changes
    actually improve hit rate (compare grouped by prompt_version / mode).
    """
    __tablename__ = "analysis_outcomes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    actionable: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Reference price at generation time (snapshot price).
    anchor_price: Mapped[float] = mapped_column(Float, nullable=False)

    # Forward closing prices — filled by cron once N trading days pass.
    close_d1: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_d3: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_d5: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_d20: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Forward returns (%), = (close_dN - anchor_price) / anchor_price * 100.
    return_d1: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_d3: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_d5: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_d20: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 5/29: snapshot of confidence + data_completeness at anchor time.
    # Nullable for legacy rows pre-this-schema-bump. Surfaces in the
    # detail-page "历史解析" card so users can see how confidence/quality
    # evolved across regenerations.
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_completeness: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 6/10 (S0): three additions for measurement honesty.
    #
    # model — which LLM produced this verdict. prompt_version alone can't
    # distinguish "same prompt, different model", which is exactly the
    # comparison the A/B mechanism (ANALYSIS_MODEL_B) needs.
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # nd_trend / nd_confidence — next_day_outlook's 看涨/看平/看跌 claim and
    # its 高/中/低 self-assessment, captured at anchor time. The single most
    # falsifiable output of the product was previously never scored.
    nd_trend: Mapped[str | None] = mapped_column(String(10), nullable=True)
    nd_confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # anchor_close — qfq close of the anchor's trading day, filled by the
    # outcomes backfill from the same kline series as close_dN. anchor_price
    # is an *unadjusted intraday* price: across an ex-dividend date its
    # return vs a qfq close is distorted (June–July is A-share dividend
    # season). Stats that want a dividend-safe basis use anchor_close.
    anchor_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 6/18: cohort 周("2026-W25"),仅预选池晋升触发的解析 anchor 带;普通
    # 自选解析 cohort=None。阶段2 按 cohort 统计这批晋升票的买卖建议命中率。
    cohort: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # 6/23 (P0): 价位预测前向埋点。LLM 每次解析在 key_table 给出买入区/目标区/
    # 止损,但从没被记进锚点、从没打过分 —— 只知道方向(nd_trend)战绩,不知道
    # 价位准不准。这 4 列照抄 nd_trend 那套"埋点+打分":buy_low/buy_high ←
    # key_table.buy_price_low/high;target_low ← sell_price_low;stop_price ←
    # 最紧止损 stop_loss_levels[0]['price']。nullable —— 只有新锚点带,老行 NULL,
    # 由 price_level_stats() 在前向 kline 到位后打分(初期 scored≈0,数据往后积累)。
    buy_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 6/23 (codex P1):recompute_returns_from_close 用当前 Kline 同批次重算
    # 这行的 return_dN 时盖的时间戳。非空 = return 已用复权安全基准清算过
    # (数据自证 clean,不再依赖"recompute 跑没跑过"的运维前提);时间戳还
    # 透出"清算有多新",对抗 qfq 历史价后续漂移(该周期性重跑)。
    returns_recomputed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_analysis_outcomes_code", "code"),
        Index("ix_analysis_outcomes_generated", "generated_at"),
    )


class Holding(Base):
    """A user's recorded position in a stock — cost basis + share count.

    Composite PK (user_id, code): one position per code per user. The
    analysis stays globally cached (not per-user) — holdings drive a
    computed "持仓对照" overlay on the detail page rather than feeding the
    LLM prompt, so cost-basis personalization doesn't multiply LLM spend
    or break the shared analysis cache.
    """
    __tablename__ = "holdings"

    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True,
    )
    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    # 买入均价 (元/股)
    cost_price: Mapped[float] = mapped_column(Float, nullable=False)
    # 持仓数量 (股) — optional; cost_price alone delivers most of the value
    shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 建仓日期 YYYY-MM-DD — optional
    opened_at: Mapped[str | None] = mapped_column(String(10), nullable=True)
    note: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
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


class PoolEntry(Base):
    """B0 (6/10): 虚拟预选池 — a paper position the system is observing
    before it's allowed to become a recommendation.

    Lifecycle: observing → recommendable → recommended (manual/UI step,
    reserved) — or → eliminated at any point. A code can re-enter after
    elimination (new row), so no unique constraint; "active" = state in
    (observing, recommendable).

    Price basis is the kline table exclusively (qfq close) — pool
    candidates from the sector_picks channel are usually NOT in any
    watchlist, so snapshots/quotes don't exist for them; klines are
    pulled per-code at entry and refreshed by the daily pool tick.

    thesis is machine-verifiable on purpose: invalidation is a price
    rule the tick can check, not a free-text catalyst it can't. The
    LLM-bull-thesis upgrade (catalyst checkpoints) is deferred until
    the price-rule loop proves out.
    """
    __tablename__ = "virtual_pool"

    # with_variant: SQLite doesn't autoincrement BigInteger PKs (only
    # INTEGER PRIMARY KEY rowid aliases), which breaks service-level
    # inserts in smoke tests. Postgres still gets BIGSERIAL.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True, autoincrement=True,
    )
    code: Mapped[str] = mapped_column(String(6), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # rules | sector_picks — which channel sourced this entry. A month of
    # outcomes per channel answers "哪个通道的票更靠谱" with data.
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="observing", index=True,
    )
    entered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    # qfq close + its kline date at entry — same series the evaluation
    # reads, so return math is dividend-safe by construction.
    entry_close: Mapped[float] = mapped_column(Float, nullable=False)
    entry_date: Mapped[str] = mapped_column(String(10), nullable=False)
    # {summary, evidence: [...], invalidation_price, invalidation_rule,
    #  sector?: str} — see services/virtual_pool.py builders.
    thesis: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # Rolling evaluation results, refreshed by the daily pool tick.
    last_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    days_observed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    state_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    eliminated_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # 6/18: ISO 周("2026-W25"),晋升成 recommendable 时写。批次(cohort)考核
    # 按周聚合成"第 N 期",每批可考核累计收益/中证500超额/买卖建议命中率。
    cohort_week: Mapped[str | None] = mapped_column(String(8), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
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
    # Backend-computed data completeness score (0-100) for the input snapshot
    # at the time this analysis was generated. Stored so we can later
    # correlate input quality with hit_rate (e.g. "low data_completeness →
    # higher miss rate" would justify gating analyses on minimum input
    # quality). See compute_data_completeness() in services/analysis.py for
    # the rubric (4 dynamic dimensions, 25 pts each).
    data_completeness: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
