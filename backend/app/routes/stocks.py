"""盯盘 view + manual snapshot trigger."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

# 4h cache TTL for the LLM-generated analysis. Mirrors the value enforced
# inside services.analysis.get_cached(); kept here so list_stocks can compute
# is_fresh in a single SQL pass without re-querying per row.
ANALYSIS_FRESH_HOURS = 4

from ..auth import require_auth
from ..db import SessionLocal, get_db
from ..models import Analysis, AnalysisOutcome, Snapshot, Watchlist
from ..services.analysis import (
    generate as analysis_generate,
    get_cached as analysis_cached,
    should_reanalyze,
)
from ..services.cron import run_daily_analysis_job, run_snapshot_job
from ..services.signals import has_strong
from ..services.users import resolve_owner

logger = logging.getLogger(__name__)

# Process-level guard so two simultaneous "手动抓取" clicks don't double-run
# the snapshot job. The lock + bool pair is intentionally simple — we only
# need correctness within a single backend replica (Railway runs 1 replica).
_snapshot_lock = threading.Lock()
_snapshot_running = False

# Same guard for the manual "批量解析" button — prevents the user from
# kicking off 23 LLM calls twice if they double-click.
_analysis_lock = threading.Lock()
_analysis_running = False

# Per-code guard + result registry for the detail-page "生成深度解析 /
# 重新生成" button. 6/28: that endpoint went async — a single analysis on a
# slow gateway (zenmux→minimax-m3 ~50s) blows past Railway's ~30s HTTP proxy
# timeout, so a synchronous request returned "Failed to fetch" even though
# the LLM call eventually succeeded in the background. Now the POST fires a
# daemon thread and returns immediately; the frontend polls /analysis/status
# and re-fetches the cached row once running flips false. code -> {running,
# error}. Bounded by the watchlist size, so it never grows unboundedly.
_single_analysis_lock = threading.Lock()
_single_analysis_jobs: dict[str, dict[str, Any]] = {}

router = APIRouter(prefix="/api/stocks", tags=["stocks"], dependencies=[Depends(require_auth)])


def _user_watchlist(db: Session, owner: int | None):
    """Watchlist query scoped to the current request's owner. Mirrors
    routes.watchlist._scoped_query — when owner is None (legacy /
    AUTH_DISABLED with no admin set), returns the unscoped query so the
    pre-account-system behaviour still works."""
    q = db.query(Watchlist)
    if owner is not None:
        q = q.filter(Watchlist.user_id == owner)
    return q


class AnalysisBrief(BaseModel):
    """Just the bits the 盯盘 list needs from the cached Analysis row.

    Mirrors the v2 key_table schema (4/27): company_tag and red_flags are
    surfaced inline in the table so a row screams '维权 + 业绩塌方' without
    needing the user to drill into the detail page.
    """
    actionable: str            # 建议买入 / 观望 / 建议卖出 / 不建议入手
    one_line_reason: str
    company_tag: str           # one-line company portrait
    red_flags: list[str]       # hard-detected risk markers
    created_at: str
    is_fresh: bool             # < 4h old
    # 5/28: top-level confidence + reason. confidence is `int | str | None`
    # — int for new rows, legacy "高"/"中"/"低" enum until migrate-
    # confidence-to-int has run, None for very old rows that never had it.
    # Frontend's confidenceBucket() normalizes all three.
    confidence: int | str | None = None
    confidence_reason: str | None = None
    # 6/3: AI-declared validity window for this verdict, e.g.
    # "3 个交易日内" / "跌破 12.50 元前". Surfaced in the 操作建议 cell
    # last line so users see decision freshness at a glance. None for
    # legacy rows pre-valid_window schema.
    valid_window: str | None = None
    # 7/2: 持仓者立场轴。盯盘池的票绝大多数是已持仓票(Rush 拍板:默认持仓、
    # 不揣测盈亏),所以列表的主 chip 改按持仓者视角展示。holding_small
    # (小幅盈/亏)正是"盈亏不构成决策因素"的那一格 —— 取它当默认持仓立场。
    # actionable 保留:显式标了未持仓的行仍用它。legacy 行无 scenario_*
    # 字段 → None,前端回落 actionable。
    holder_direction: str | None = None   # scenario_direction.holding_small: 看多/看空/中性
    holder_advice: str | None = None      # scenario_advice.holding_small 原文


class StockRow(BaseModel):
    code: str
    name: str
    exchange: str
    last_ts: str | None
    # 今日 columns kept (the Phase 7 ask removed "价格" + "信号" from the
    # list display, but the data stays — UI just doesn't render those
    # columns anymore. change_pct labels as "今日涨跌" on frontend.)
    # 7/2: price 列回归 — 最近一次抓取的现价(交易时段内 quotes_5min 五分钟
    # 一跳,近实时)。
    price: float | None
    change_pct: float | None
    # Phase 7 new columns: 3-day rolling metrics
    change_pct_3d: float | None       # 3日涨幅 %
    turnover_rate_3d: float | None    # 3日累计换手率 %
    net_flow_3d: float | None         # 3日主力净流入 元
    # Industry context (top-of-row chips on detail page; condensed name in list)
    industry_name: str | None
    industry_pe_pctile: float | None
    industry_change_3d_pctile: float | None
    industry_flow_3d_pctile: float | None
    # Signals stay in the response so the frontend can keep tinting strong
    # signal rows red even after dropping the dedicated 信号 column.
    signals: list[str]
    has_strong_signal: bool
    on_lhb: bool
    starred: bool                     # user-marked "特别关注"
    analysis: AnalysisBrief | None  # null when never generated


@router.get("", response_model=list[StockRow])
def list_stocks(
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Latest snapshot per watched code, joined with watchlist + cached
    analysis. Watchlist is scoped to the authenticated user; snapshots and
    analyses are shared market-level state so we just join through."""
    owner = resolve_owner(user_id, db)
    watch = {w.code: w for w in _user_watchlist(db, owner).all()}
    if not watch:
        return []

    # Latest-per-code: subquery for max(id) per code, then join.
    subq = (
        db.query(Snapshot.code, Snapshot.id.label("id"))
        .order_by(Snapshot.code, desc(Snapshot.id))
        .distinct(Snapshot.code)
        .subquery()
    )
    # The DISTINCT ON above is Postgres-only. Fallback for SQLite (smoke tests):
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        latest_ids: list[int] = []
        for code in watch.keys():
            r = (
                db.query(Snapshot.id)
                .filter(Snapshot.code == code)
                .order_by(desc(Snapshot.id))
                .first()
            )
            if r:
                latest_ids.append(r[0])
        snaps = db.query(Snapshot).filter(Snapshot.id.in_(latest_ids)).all() if latest_ids else []
    else:
        snaps = db.query(Snapshot).join(subq, Snapshot.id == subq.c.id).all()

    by_code = {s.code: s for s in snaps}

    # Junk-row fallback: if the latest snapshot for a code has neither a
    # price nor a main_net_flow (e.g., a 16:00 hourly job ran with all
    # akshare endpoints failing and wrote a row of nulls), surface the
    # most recent row that *does* have data so the 盯盘 list keeps
    # showing yesterday's close instead of going blank.
    for code in list(by_code.keys()):
        s = by_code[code]
        if s.price is None and s.main_net_flow is None:
            good = (
                db.query(Snapshot)
                .filter(
                    Snapshot.code == code,
                    (Snapshot.price.isnot(None)) | (Snapshot.main_net_flow.isnot(None)),
                )
                .order_by(desc(Snapshot.id))
                .first()
            )
            if good is not None:
                by_code[code] = good

    # One-shot pull of every analysis row for the watched codes.
    analyses = {
        a.code: a
        for a in db.query(Analysis).filter(Analysis.code.in_(list(watch.keys()))).all()
    }
    # 6/3: anchor snapshot batch prefetch — needed for should_reanalyze's
    # price-move / signal-change comparison. Snapshot ids referenced by
    # any existing analysis row, batched into a single IN query.
    anchor_ids = [a.snapshot_id for a in analyses.values()
                  if a.snapshot_id is not None]
    anchor_snaps = (
        {s.id: s for s in db.query(Snapshot).filter(Snapshot.id.in_(anchor_ids)).all()}
        if anchor_ids else {}
    )

    def _brief(code: str) -> AnalysisBrief | None:
        a = analyses.get(code)
        if a is None:
            return None
        kt = a.key_table or {}
        # Schema v2 invalidation: rows from the old schema (no company_tag)
        # are treated as missing so batch_analysis re-generates them with
        # the new structure. No DB cleanup needed.
        if "company_tag" not in kt:
            return None
        created = a.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        # confidence may be int (new), str (legacy enum), or missing.
        # Pass through as-is; frontend normalizes via confidenceBucket().
        raw_conf = kt.get("confidence")
        conf_val: int | str | None
        if isinstance(raw_conf, (int, str)) and raw_conf != "":
            conf_val = raw_conf
        else:
            conf_val = None
        # 6/3: is_fresh now mirrors should_reanalyze — the same logic
        # that drives the smart cron decides the visual badge. respect_
        # cooldown=False so a 5-min-old row that already has a real
        # trigger (price moved 2%) still flags as 已过期 immediately
        # for the user, even though the cron will hold off until 30 min
        # passes. Net effect: list 和 cron 对"过期"的定义保持一致。
        snap = by_code.get(code)
        anchor = (anchor_snaps.get(a.snapshot_id)
                  if a.snapshot_id is not None else None)
        needs_repaint, _ = should_reanalyze(snap, a, anchor, respect_cooldown=False)
        sdir = kt.get("scenario_direction") or {}
        sadv = kt.get("scenario_advice") or {}
        return AnalysisBrief(
            actionable=str(kt.get("actionable") or ""),
            one_line_reason=str(kt.get("one_line_reason") or ""),
            company_tag=str(kt.get("company_tag") or ""),
            red_flags=list(kt.get("red_flags") or []),
            created_at=created.isoformat() if created else "",
            is_fresh=not needs_repaint,
            confidence=conf_val,
            confidence_reason=(str(kt["confidence_reason"])
                               if kt.get("confidence_reason") else None),
            valid_window=(str(kt["valid_window"])
                          if kt.get("valid_window") else None),
            holder_direction=(str(sdir["holding_small"])
                              if isinstance(sdir, dict) and sdir.get("holding_small")
                              else None),
            holder_advice=(str(sadv["holding_small"])
                           if isinstance(sadv, dict) and sadv.get("holding_small")
                           else None),
        )

    rows: list[StockRow] = []
    for code, w in watch.items():
        s = by_code.get(code)
        signals = (s.signals if s else None) or []
        rows.append(StockRow(
            code=code,
            name=w.name,
            exchange=w.exchange,
            last_ts=s.ts.isoformat() if s else None,
            price=(s.price if s else None),
            change_pct=(s.change_pct if s else None),
            change_pct_3d=(s.change_pct_3d if s else None),
            turnover_rate_3d=(s.turnover_rate_3d if s else None),
            net_flow_3d=(s.net_flow_3d if s else None),
            industry_name=(s.industry_name if s else None),
            industry_pe_pctile=(s.industry_pe_pctile if s else None),
            industry_change_3d_pctile=(s.industry_change_3d_pctile if s else None),
            industry_flow_3d_pctile=(s.industry_flow_3d_pctile if s else None),
            signals=signals,
            has_strong_signal=has_strong(signals),
            on_lhb=bool(s.lhb) if s else False,
            starred=bool(getattr(w, "starred", False)),
            analysis=_brief(code),
        ))
    # Order key, most-important first:
    #   1. Starred rows (user said "watch this closely") — float to top of
    #      whatever bucket the frontend groups them into
    #   2. Strong-signal rows (limit_up/down, big notice, lhb)
    #   3. Larger |change_pct| comes first
    rows.sort(key=lambda r: (
        not r.starred,
        not r.has_strong_signal,
        -abs(r.change_pct or 0),
    ))
    return rows


class SnapshotTriggerResult(BaseModel):
    started: bool
    already_running: bool = False


class SnapshotStatus(BaseModel):
    running: bool


def _run_snapshot_in_background(post_close: bool) -> None:
    global _snapshot_running
    try:
        run_snapshot_job(post_close=post_close)
    except Exception:
        logger.exception("background snapshot job failed")
    finally:
        with _snapshot_lock:
            _snapshot_running = False


# ---------------------------------------------------------------------------
# Public hit-rate summary — UI shows historical accuracy of buy/sell verdicts
# alongside the actionable badge so users see "AI 历史命中 60% (n=48)"
# instead of having to take the verdict on faith. Calls outcomes_svc and
# caches the result for 30 min (sample sizes shift slowly compared to
# how often the list re-renders).
# ---------------------------------------------------------------------------

class HitRateBucket(BaseModel):
    n: int
    hit_rate: float | None
    avg_return_d5: float | None
    # S2 (6/10): honest-stats fields surfaced to the UI. The raw hit_rate
    # conflates market beta (sell verdicts cluster on red days) and
    # clustering inflation (smart cron re-anchors trending stocks). The UI
    # should lead with these, not the raw number.
    n_unique: int | None = None           # distinct (code, day) anchors
    hit_rate_dedup: float | None = None   # hit rate on last-anchor-per-day set
    excess_return_d5: float | None = None # avg return minus same-day all-anchor median


class HitRateSummary(BaseModel):
    """Public-facing snippet for the UI. Filters to v2.5-single (debate
    sample is too small to publish) and buy/sell only (others have no
    directional claim so no hit_rate)."""
    by_actionable: dict[str, HitRateBucket]
    # 7/2: 持仓立场轴的战绩 — scenario_hit_stats 的 4 个情境桶(key =
    # not_holding / holding_big_gain / holding_small / holding_big_loss)。
    # 详情页大字结论换成持仓立场后,命中率口径必须跟着切,否则又是
    # 大字说"减仓"、战绩挂"不建议入手"的口径错位。
    by_scenario: dict[str, HitRateBucket] = {}
    total_scored: int
    cached_at: str


_hit_rate_cache: dict = {"data": None, "ts": 0.0}
_HIT_RATE_CACHE_TTL = 30 * 60  # 30 min


@router.get("/hit-rate-summary", response_model=HitRateSummary)
def get_hit_rate_summary():
    """Hit-rate summary surfaced in list view tooltips + detail page.
    Filtered to v2.5-single buy/sell. 30-min in-process cache."""
    import time
    from ..services import outcomes as outcomes_svc
    now = time.time()
    if (_hit_rate_cache["data"] is not None
            and (now - _hit_rate_cache["ts"]) < _HIT_RATE_CACHE_TTL):
        return _hit_rate_cache["data"]

    raw = outcomes_svc.hit_rate_stats()
    # 7/2: 接受所有 -single 版本(v2.5/v2.6/...) — 公示数是信任信号,按
    # 版本切的 A/B 看 diag outcomes-stats。debate/deep 桶照旧不公示。
    # 同一 actionable 会有多个版本桶 → 按 n / n_unique 加权合并(对
    # 计数型比率是精确合并)。
    def _wavg(pairs: list[tuple[float | None, int]]) -> float | None:
        num = sum(v * w for v, w in pairs if v is not None and w)
        den = sum(w for v, w in pairs if v is not None and w)
        return round(num / den, 1) if den else None

    merged: dict[str, list[dict]] = {}
    for b in raw["buckets"]:
        if not str(b["prompt_version"]).endswith("-single"):
            continue
        if b["actionable"] not in ("建议买入", "建议卖出"):
            continue
        merged.setdefault(b["actionable"], []).append(b)

    by_actionable: dict[str, HitRateBucket] = {}
    for actionable, bs in merged.items():
        n = sum(b["n"] for b in bs)
        n_unique = sum(b.get("n_unique") or 0 for b in bs)
        by_actionable[actionable] = HitRateBucket(
            n=n,
            hit_rate=_wavg([(b["hit_rate"], b["n"]) for b in bs]),
            avg_return_d5=_wavg([(b["avg_return_d5"], b["n"]) for b in bs]),
            n_unique=n_unique or None,
            hit_rate_dedup=_wavg(
                [(b.get("hit_rate_dedup"), b.get("n_unique") or 0) for b in bs]),
            excess_return_d5=_wavg([(b.get("excess_return_d5"), b["n"]) for b in bs]),
        )
    by_scenario: dict[str, HitRateBucket] = {}
    try:
        sraw = outcomes_svc.scenario_hit_stats()
        for b in sraw["scenarios"]:
            by_scenario[b["scenario"]] = HitRateBucket(
                n=b["n_scored"],
                hit_rate=b["hit_rate"],
                avg_return_d5=b["avg_return_d5"],
                n_unique=b.get("n_unique"),
                hit_rate_dedup=b.get("hit_rate_dedup"),
                excess_return_d5=b.get("excess_return_d5"),
            )
    except Exception:
        logger.exception("scenario_hit_stats failed; by_scenario omitted")

    payload = HitRateSummary(
        by_actionable=by_actionable,
        by_scenario=by_scenario,
        total_scored=raw["total_scored"],
        cached_at=datetime.now(timezone.utc).isoformat(),
    )
    _hit_rate_cache["data"] = payload
    _hit_rate_cache["ts"] = now
    return payload


class ActionItem(BaseModel):
    code: str
    name: str
    type: str       # stop_loss_breach | sell_verdict | sell_stance | valid_window_expired | signal_alert
    severity: str   # urgent | warn
    message: str


class ActionItemsOut(BaseModel):
    items: list[ActionItem]
    # 7/2 起 = 按默认持仓口径检查的 code 数(自选 ∪ Holding − 显式未持),
    # 不再只数录了成本价的 Holding。字段名保留兼容前端。
    checked_holdings: int


@router.get("/action-items", response_model=ActionItemsOut)
def get_action_items(
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """S1 (6/10): 今日需行动 — holdings-aware sell triggers. 7/2 持仓立场
    轴:检查范围扩成默认持仓全集(自选 ∪ Holding − 漏斗显式未持),新增
    sell_stance(用户象限 scenario_direction 看空)。Computed on request
    (spec excludes push; the 盯盘 banner is the push surrogate).
    NOTE: declared before /{code} so the literal path wins routing."""
    from ..services import action_items as action_items_svc
    owner = resolve_owner(user_id, db)
    return action_items_svc.compute_for_user(db, owner)


@router.post("/snapshot", response_model=SnapshotTriggerResult)
def trigger_snapshot(post_close: bool = False):
    """Kick off the snapshot job in a background thread and return immediately.

    The job collects 4 akshare endpoints per code; for 20+ codes it routinely
    takes 30–90s. Returning sync would let the browser/edge proxy time out
    even though the job itself succeeds. The frontend polls `/api/stocks`
    while we run.
    """
    global _snapshot_running
    with _snapshot_lock:
        if _snapshot_running:
            return SnapshotTriggerResult(started=False, already_running=True)
        _snapshot_running = True

    threading.Thread(
        target=_run_snapshot_in_background,
        args=(post_close,),
        daemon=True,
        name="snapshot-job",
    ).start()
    return SnapshotTriggerResult(started=True)


@router.get("/snapshot/status", response_model=SnapshotStatus)
def snapshot_status():
    """Lets the frontend poll whether a manual/background job is still running."""
    return SnapshotStatus(running=_snapshot_running)


# --- Batch LLM analysis ---------------------------------------------------
# These two routes are intentionally above /{code} below so the static
# /analysis/batch path doesn't get parsed as code="analysis".


class AnalysisBatchResult(BaseModel):
    started: bool
    already_running: bool = False


class AnalysisBatchStatus(BaseModel):
    running: bool


def _run_analysis_batch_in_background(only_missing: bool, codes: list[str] | None):
    global _analysis_running
    try:
        # only_missing=True: fill in 待生成 only (skip any v2 cached row).
        # only_missing=False: force regenerate every code.
        # codes: owner 隔离过的当前用户自选(见 trigger_batch_analysis)。
        run_daily_analysis_job(only_stale=False, only_missing=only_missing, codes=codes)
    except Exception:
        logger.exception("batch analysis job failed")
    finally:
        with _analysis_lock:
            _analysis_running = False


@router.post("/analysis/batch", response_model=AnalysisBatchResult)
def trigger_batch_analysis(
    only_missing: bool = True,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Generate LLM analyses for the watchlist.

    Default `only_missing=true` matches the 盯盘 button's "fill 待生成 only"
    behavior — clicking when 0 are pending should pass `only_missing=false`
    explicitly to force-regen every code (the frontend confirms first).

    Fire-and-forget: launches a daemon thread, returns immediately. The
    frontend polls /analysis/batch/status + /api/stocks to follow progress
    (rows light up with their actionable verdict as each LLM call lands).

    6/24 安全(codex P1):**只分析当前用户自己的自选**,不再扫全局 watchlist
    —— 否则一个用户点"批量分析"会烧掉所有人自选的 LLM 额度。owner 解析 + 取
    自己的 codes 在请求线程里做(后台线程没有 auth 上下文),再传进后台 job。"""
    global _analysis_running
    owner = resolve_owner(user_id, db)
    codes = sorted({w.code for w in _user_watchlist(db, owner).all()})

    with _analysis_lock:
        if _analysis_running:
            return AnalysisBatchResult(started=False, already_running=True)
        _analysis_running = True

    threading.Thread(
        target=_run_analysis_batch_in_background,
        kwargs={"only_missing": only_missing, "codes": codes},
        daemon=True,
        name="batch-analysis",
    ).start()
    return AnalysisBatchResult(started=True)


@router.get("/analysis/batch/status", response_model=AnalysisBatchStatus)
def batch_analysis_status():
    return AnalysisBatchStatus(running=_analysis_running)


class StockDetail(BaseModel):
    code: str
    name: str
    exchange: str
    last_ts: str | None
    price: float | None
    change_pct: float | None
    main_net_flow: float | None
    # Phase 7: surface 3-day + industry context on the detail page so the
    # KeyTableCard can show "行业平均 PE / 行业 PE 分位 / 3 日资金分位" chips.
    change_pct_3d: float | None
    turnover_rate_3d: float | None
    net_flow_3d: float | None
    pe_ratio: float | None
    pb_ratio: float | None
    industry_name: str | None
    industry_pe_pctile: float | None
    industry_change_3d_pctile: float | None
    industry_flow_3d_pctile: float | None
    industry_pe_avg: float | None
    industry_pb_avg: float | None
    signals: list[str]
    news: list[dict[str, Any]]
    notices: list[dict[str, Any]]
    lhb: dict[str, Any] | None


@router.get("/{code}", response_model=StockDetail)
def stock_detail(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Latest snapshot detail for one stock. 404 if the code isn't in the
    current user's watchlist — keeps user A from poking at codes only user
    B watches just because they happen to share market data."""
    owner = resolve_owner(user_id, db)
    w = _user_watchlist(db, owner).filter(Watchlist.code == code).first()
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")
    s = (
        db.query(Snapshot)
        .filter(Snapshot.code == code)
        .order_by(desc(Snapshot.id))
        .first()
    )
    # Same junk-row fallback as list_stocks: if the latest snapshot is
    # all-null on price+flow, walk back to the most recent good one.
    if s is not None and s.price is None and s.main_net_flow is None:
        good = (
            db.query(Snapshot)
            .filter(
                Snapshot.code == code,
                (Snapshot.price.isnot(None)) | (Snapshot.main_net_flow.isnot(None)),
            )
            .order_by(desc(Snapshot.id))
            .first()
        )
        if good is not None:
            s = good
    return StockDetail(
        code=code,
        name=w.name,
        exchange=w.exchange,
        last_ts=s.ts.isoformat() if s else None,
        price=(s.price if s else None),
        change_pct=(s.change_pct if s else None),
        main_net_flow=(s.main_net_flow if s else None),
        change_pct_3d=(s.change_pct_3d if s else None),
        turnover_rate_3d=(s.turnover_rate_3d if s else None),
        net_flow_3d=(s.net_flow_3d if s else None),
        pe_ratio=(s.pe_ratio if s else None),
        pb_ratio=(s.pb_ratio if s else None),
        industry_name=(s.industry_name if s else None),
        industry_pe_pctile=(s.industry_pe_pctile if s else None),
        industry_change_3d_pctile=(s.industry_change_3d_pctile if s else None),
        industry_flow_3d_pctile=(s.industry_flow_3d_pctile if s else None),
        industry_pe_avg=(s.industry_pe_avg if s else None),
        industry_pb_avg=(s.industry_pb_avg if s else None),
        signals=(s.signals if s else None) or [],
        news=(s.news if s else None) or [],
        notices=(s.notices if s else None) or [],
        lhb=(s.lhb if s else None),
    )


# --- Phase 3: deep analysis ----------------------------------------------


class AnalysisOut(BaseModel):
    code: str
    key_table: dict[str, Any]
    deep_analysis: str
    model: str
    strategy: str
    created_at: str
    snapshot_id: int | None
    is_fresh: bool
    # "single" | "debate" — drives the 🔬 深度解析结果 banner + scroll-to
    # behavior on the detail page. None for legacy rows pre-Phase 10.5.
    mode: str | None = None
    # 5/28: data completeness score (0-100) computed at analysis time. None
    # for legacy rows pre-this-schema-bump. Detail page shows it in the
    # footnote so users can mentally weight the verdict.
    data_completeness: int | None = None
    # 6/26: 缓存"不是最新"的原因 tag(should_reanalyze 返回):price_move /
    # signal_change / stale / no_anchor。前端据此显准确文案,不把 price_move
    # (行情大动)误标成 "缓存已过期 (>4h)"。
    stale_reason: str | None = None

    @classmethod
    def from_row(
        cls, row: Analysis, is_fresh: bool, stale_reason: str | None = None,
    ) -> "AnalysisOut":
        return cls(
            code=row.code,
            key_table=row.key_table,
            deep_analysis=row.deep_analysis,
            model=row.model,
            strategy=row.strategy,
            created_at=row.created_at.isoformat() if row.created_at else "",
            snapshot_id=row.snapshot_id,
            is_fresh=is_fresh,
            mode=getattr(row, "mode", None) or "single",
            data_completeness=getattr(row, "data_completeness", None),
            stale_reason=stale_reason,
        )


@router.get("/{code}/analysis", response_model=AnalysisOut | None)
def get_analysis(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Return cached analysis (shared across users — see plan: "Analysis
    缓存：全局共享") if the code is in the current user's watchlist. Codes
    they don't watch get a 404 to avoid leaking which stocks others care
    about."""
    owner = resolve_owner(user_id, db)
    if not _user_watchlist(db, owner).filter(Watchlist.code == code).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")
    row = db.query(Analysis).filter(Analysis.code == code).first()
    if row is None:
        return None
    # 6/3: align is_fresh with the list view + smart cron's freshness
    # logic. Detail page already had a 4h cutoff via analysis_cached;
    # switching to should_reanalyze means an actively-moving stock
    # shows "已过期" earlier and a quiet stock stays fresh past 4h.
    latest_snap = (
        db.query(Snapshot).filter(Snapshot.code == code)
        .order_by(desc(Snapshot.id)).first()
    )
    anchor_snap = (
        db.query(Snapshot).filter(Snapshot.id == row.snapshot_id).first()
        if row.snapshot_id is not None else None
    )
    needs_repaint, reason = should_reanalyze(latest_snap, row, anchor_snap,
                                             respect_cooldown=False)
    return AnalysisOut.from_row(
        row, is_fresh=not needs_repaint,
        stale_reason=(reason if needs_repaint else None),
    )


class AnalysisJobResult(BaseModel):
    started: bool
    already_running: bool = False


class AnalysisJobStatus(BaseModel):
    running: bool
    # Short error string when the last run failed; None on success / while
    # running. The frontend surfaces it instead of a silent stall.
    error: str | None = None


def _run_single_analysis_in_background(code: str, mode: str, force: bool):
    """Runs one analysis in a daemon thread with its own DB session (the
    request's session is gone by the time this executes). Records the
    outcome in _single_analysis_jobs so the /status poll can report it."""
    db = SessionLocal()
    err: str | None = None
    try:
        analysis_generate(db, code, mode=mode, force=force)
    except ValueError as e:
        # code vanished from watchlist mid-flight — treat as a soft error.
        err = str(e)
    except Exception as e:  # noqa: BLE001 — surface any LLM/gateway failure
        logger.exception("single analysis job failed for %s", code)
        err = str(e)
    finally:
        db.close()
        with _single_analysis_lock:
            job = _single_analysis_jobs.get(code)
            if job is not None:
                job["running"] = False
                job["error"] = err


@router.post("/{code}/analysis", response_model=AnalysisJobResult)
def generate_analysis(
    code: str,
    mode: str = "single",
    force: bool = False,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Kick off a force-regenerate in the background. Scoped to the user's
    watchlist (404 if not theirs) so users can't burn LLM tokens for codes
    they don't follow.

    6/28: async fire-and-forget. A single analysis on a slow gateway can
    take ~50s; Railway's HTTP proxy kills requests past ~30s, so the old
    synchronous handler returned "Failed to fetch" to the browser even
    though the LLM call kept running and eventually persisted its row. Now
    we launch a daemon thread and return immediately; the frontend polls
    `GET /{code}/analysis/status` and re-fetches the cached row (via
    `GET /{code}/analysis`) once running flips false.

    `?mode=debate` runs the bull/bear/judge debate pipeline (3 LLM calls,
    sharper red-flag detection). Default is `single` (one call).

    `?mode=deep` (6/29) 单股深挖:走 OpenAI 兼容协议 + thinking 模型
    (qwen3.7-max,~90s),用户主动触发。需配 ANALYSIS_DEEP_MODEL,否则 503。

    `?force=true` (5/29) bypasses the snapshot-id cache: even if the
    cached analysis is based on the same snapshot, re-call the LLM.
    Frontend detail-page "重新生成" button sets this to true (user
    explicitly wants a fresh take); batch_analyze leaves it false so
    repeated batch runs on unchanged snapshots reuse results.
    """
    owner = resolve_owner(user_id, db)
    if not _user_watchlist(db, owner).filter(Watchlist.code == code).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")
    # deep 走和 single/debate 一样的异步后台路径 —— 尤其重要:deep ~90s 远超
    # Railway 30s HTTP 代理上限, 同步必被杀, 后台线程 + 轮询正好兜住。
    if mode not in ("single", "debate", "deep"):
        raise HTTPException(status_code=400, detail="mode must be single, debate or deep")

    with _single_analysis_lock:
        job = _single_analysis_jobs.get(code)
        if job is not None and job["running"]:
            return AnalysisJobResult(started=False, already_running=True)
        # Mark running synchronously in the request thread so a /status poll
        # racing right behind the POST never sees a stale running=false.
        _single_analysis_jobs[code] = {"running": True, "error": None}

    threading.Thread(
        target=_run_single_analysis_in_background,
        kwargs={"code": code, "mode": mode, "force": force},
        daemon=True,
        name=f"analysis-{code}",
    ).start()
    return AnalysisJobResult(started=True)


@router.get("/{code}/analysis/status", response_model=AnalysisJobStatus)
def single_analysis_status(code: str):
    """Poll target for the detail-page generate/regenerate flow. No auth
    (mirrors /analysis/batch/status) — leaks only a boolean + error string
    for a code the caller already named."""
    with _single_analysis_lock:
        job = _single_analysis_jobs.get(code)
        if job is None:
            return AnalysisJobStatus(running=False, error=None)
        return AnalysisJobStatus(running=job["running"], error=job["error"])


class AnalysisHistoryItem(BaseModel):
    """One historical anchor + its forward returns. Powers the detail
    page's 历史解析 collapsible card so users can see how the AI's
    verdict + confidence shifted across regenerations, alongside the
    actual returns those anchors achieved."""
    generated_at: str
    actionable: str
    anchor_price: float
    confidence: int | None = None
    data_completeness: int | None = None
    # Forward returns in %. None when the horizon hasn't elapsed yet
    # (filled in by _outcomes_tick once the trading days pass).
    return_d1: float | None = None
    return_d3: float | None = None
    return_d5: float | None = None
    mode: str | None = None
    prompt_version: str | None = None

    @classmethod
    def from_outcome(cls, o: AnalysisOutcome) -> "AnalysisHistoryItem":
        return cls(
            generated_at=o.generated_at.isoformat() if o.generated_at else "",
            actionable=o.actionable or "",
            anchor_price=o.anchor_price,
            confidence=o.confidence,
            data_completeness=o.data_completeness,
            return_d1=o.return_d1,
            return_d3=o.return_d3,
            return_d5=o.return_d5,
            mode=o.mode,
            prompt_version=o.prompt_version,
        )


@router.get("/{code}/analysis-history", response_model=list[AnalysisHistoryItem])
def get_analysis_history(
    code: str,
    limit: int = 10,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Return the last N anchored analyses for this code, most recent
    first. Joined with their (eventually-backfilled) forward returns so
    the frontend can show the trajectory of past verdicts AND how they
    played out.

    Ownership-scoped to the user's watchlist for the same reason as
    `generate_analysis` — don't surface anchor data for codes the user
    isn't tracking. limit capped at 50 to keep responses small."""
    owner = resolve_owner(user_id, db)
    if not _user_watchlist(db, owner).filter(Watchlist.code == code).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")
    limit = max(1, min(50, limit))
    outcomes = (
        db.query(AnalysisOutcome)
        .filter(AnalysisOutcome.code == code)
        .order_by(desc(AnalysisOutcome.generated_at))
        .limit(limit)
        .all()
    )
    return [AnalysisHistoryItem.from_outcome(o) for o in outcomes]


class PeerRow(BaseModel):
    """同业可比确定性卡一行。本股 is_self=True;跨行业 fallback peer
    is_cross_industry=True 且财务列可能 None(不在 watchlist 没财报)。"""
    code: str
    name: str | None = None
    pe_ratio: float | None = None
    pb_ratio: float | None = None
    change_pct: float | None = None       # 今日%
    revenue_yoy: float | None = None      # 营收同比增速 %
    roe: float | None = None              # %
    gross_margin: float | None = None     # 毛利率 %
    is_self: bool = False
    is_cross_industry: bool = False


@router.get("/{code}/sell-risk")
def get_sell_risk(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """卖出线 S3:该票当前的客观风险信号(live,非缓存)。返回 {level, triggers:
    [{key, reason}]} 或 null(无风险)。**有效性验证中、不对客承诺** —— 前端按
    持仓盈亏档融合成 护利 / 护本 / 观察 的动作,框「客观提示·验证中」。
    Ownership-scoped 到用户 watchlist。"""
    owner = resolve_owner(user_id, db)
    if not _user_watchlist(db, owner).filter(Watchlist.code == code).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")
    from ..services.sell_signal import sell_risk_signal
    return sell_risk_signal(db, code)


@router.get("/{code}/peers", response_model=list[PeerRow])
def get_peers(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """同业可比确定性卡数据 — 同行业 PE 最接近本股的 5 支 peer + 本股,
    每行带 PE/PB/今日% (snapshot) + 营收增速/ROE/毛利率 (financials)。

    纯查数,不过 LLM(确定性、零幻觉)。跟 prompt 的同业可比段共用底层
    compute_peers() 选取逻辑(一鱼两吃)。跨行业 fallback peer 可能不在
    watchlist → 无财报 → 财务列 None,前端显示"—"。

    Ownership-scoped 到用户 watchlist(同 analysis-history)。"""
    owner = resolve_owner(user_id, db)
    if not _user_watchlist(db, owner).filter(Watchlist.code == code).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")

    from ..services.analysis import compute_peers
    from ..services.financials import batch_latest_for_codes

    s = (
        db.query(Snapshot)
        .filter(Snapshot.code == code)
        .order_by(desc(Snapshot.id))
        .first()
    )
    rows = compute_peers(s)
    if not rows:
        return []

    all_codes = [r["code"] for r in rows]
    fin_map = batch_latest_for_codes(all_codes, n=1)
    # 全局 watchlist code→name (name 全局一致,不限 owner — 跨行业 peer
    # 可能在别人 watchlist 里有名字)。从没人加过的 peer → None,前端兜底显 code。
    name_rows = (
        db.query(Watchlist.code, Watchlist.name)
        .filter(Watchlist.code.in_(all_codes))
        .distinct()
        .all()
    )
    name_map = {c: n for c, n in name_rows}

    out: list[PeerRow] = []
    for r in rows:
        fin = fin_map.get(r["code"], [])
        f0 = fin[0] if fin else None
        out.append(PeerRow(
            code=r["code"],
            name=name_map.get(r["code"]),
            pe_ratio=r["pe_ratio"],
            pb_ratio=r["pb_ratio"],
            change_pct=r["change_pct"],
            revenue_yoy=(f0.revenue_yoy if f0 else None),
            roe=(f0.roe if f0 else None),
            gross_margin=(f0.gross_margin if f0 else None),
            is_self=r["is_self"],
            is_cross_industry=r["is_cross_industry"],
        ))
    return out
