"""今日需行动 — 持仓感知的卖出触发 (S1).

7/2 持仓立场轴:检查范围从"录了成本价的 Holding"扩成**默认持仓**全集
(Rush 拍板 — 盯盘池绝大比例是持仓票):用户自选列表里的每只票都查,
除非用户在漏斗里显式标了未持仓(FunnelChoice.held=False)。

For each such code, check the (globally shared) latest snapshot + cached
analysis for conditions that mean "今天该看一眼这支票了":

1. stop_loss_breach — current price at/below any of the analysis's
   stop_loss_levels. The single most direct "act now" signal.
2. sell_verdict — the cached analysis verdict is 建议卖出.
2b. sell_stance — 用户所在持仓象限(录了成本价 → 按浮盈亏算;漏斗标了
   盈亏档 → 按标的;都没有 → holding_small)的 scenario_direction 是
   看空。这补上了 actionable=不建议入手(买家视角)但持仓者早该减仓的
   漏洞(603986 案例)。actionable 已是 建议卖出 时不重复报。
3. valid_window_expired — the analysis declared a validity window and the
   machine-checkable kinds (跌破 X.XX / N 个交易日内 / 本周内) have
   verifiably lapsed. Event-driven windows ("出 Q3 财报前") can't be
   verified and are skipped — no false alarms.
4. signal_alert — a STRONG signal appeared on the latest snapshot that
   wasn't present on the analysis's anchor snapshot (a development the
   current verdict didn't know about).

Spec says no push notifications — the 盯盘 page's「今日需行动」section is
the push surrogate, so this is computed on request, read-only, no cron.

Pure evaluation logic is separated from DB orchestration for testability:
evaluate_code() takes plain rows, compute_for_user() does the queries.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..models import Analysis, FunnelChoice, Holding, Snapshot, Watchlist
from .signals import STRONG_SIGNALS

logger = logging.getLogger(__name__)

# Strong signals that are bad news for a holder → urgent. The rest of the
# strong set (limit_up, breakout_20d, lhb, important_notice) still warrants
# a look but isn't an alarm bell — warn.
BEARISH_STRONG = {"limit_down", "below_ma60"}


# ---------------------------------------------------------------------------
# valid_window parsing — only the patterns the prompt explicitly mandates.
# Returns (expired: bool | None, why: str | None); None = not machine-
# checkable (event-driven windows), caller skips.
# ---------------------------------------------------------------------------

_RE_PRICE_FLOOR = re.compile(r"跌破\s*([0-9]+(?:\.[0-9]+)?)")
_RE_TRADING_DAYS = re.compile(r"(\d+)(?:\s*[-–~]\s*(\d+))?\s*个?交易日")


def _trading_days_between(start: date, end: date) -> int:
    """Weekdays strictly after `start` up to and including `end`.
    Holiday-blind on purpose — worst case a window is called expired a
    holiday early, which just prompts a regenerate; not worth a calendar
    dependency."""
    if end <= start:
        return 0
    n = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def check_valid_window(
    valid_window: str | None,
    generated_at: datetime,
    current_price: float | None,
    now: datetime | None = None,
) -> tuple[bool | None, str | None]:
    """Evaluate whether a declared validity window has lapsed.

    Checkable patterns (per the prompt's mandated formats):
      - "跌破 X.XX (元)前"  → expired when current_price < X
      - "N 个交易日内" / "N-M 个交易日内" → expired when elapsed trading
        days > N (upper bound when a range)
      - "本周内" → expired when now is in a later ISO week

    Returns (expired, why). (None, None) = can't verify (event windows).
    """
    if not valid_window:
        return None, None
    now = now or datetime.now(timezone.utc)
    gen = generated_at if generated_at.tzinfo else generated_at.replace(tzinfo=timezone.utc)

    m = _RE_PRICE_FLOOR.search(valid_window)
    if m and current_price is not None:
        floor = float(m.group(1))
        if current_price < floor:
            return True, f"现价 {current_price:.2f} 已跌破声明的 {floor:.2f}"
        return False, None

    m = _RE_TRADING_DAYS.search(valid_window)
    if m:
        n = int(m.group(2) or m.group(1))  # upper bound of "1-3"
        elapsed = _trading_days_between(gen.date(), now.date())
        if elapsed > n:
            return True, f"声明 {n} 个交易日内，已过 {elapsed} 个交易日"
        return False, None

    if "本周" in valid_window:
        gen_week = gen.isocalendar()[:2]
        now_week = now.isocalendar()[:2]
        if now_week > gen_week:
            return True, "声明本周内有效，已跨周"
        return False, None

    return None, None  # event-driven / free text — not checkable


# ---------------------------------------------------------------------------
# Per-code evaluation (pure)
# ---------------------------------------------------------------------------

def evaluate_code(
    code: str,
    name: str | None,
    snap: Snapshot | None,
    analysis: Analysis | None,
    anchor_snap: Snapshot | None,
    quadrant: str = "holding_small",
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Run the checks for one (default-)held code. `quadrant` is the user's
    持仓情境 key (holding_big_gain / holding_small / holding_big_loss) used
    by the sell_stance check. Returns 0..n action items:
    {code, name, type, severity, message}. severity ∈ {urgent, warn}."""
    items: list[dict[str, Any]] = []
    display = name or code
    price = snap.price if snap is not None else None

    kt: dict[str, Any] = {}
    if analysis is not None and isinstance(analysis.key_table, dict):
        kt = analysis.key_table

    def _age_days(dt: datetime) -> int:
        d = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return max(0, ((now or datetime.now(timezone.utc)) - d).days)

    # 1. stop-loss breach — report the DEEPEST breached level (most severe).
    if price is not None and kt.get("stop_loss_levels"):
        breached = [
            lv for lv in kt["stop_loss_levels"]
            if isinstance(lv, dict) and isinstance(lv.get("price"), (int, float))
            and price <= lv["price"]
        ]
        if breached:
            worst = min(breached, key=lambda lv: lv["price"])
            msg = (
                f"现价 {price:.2f} 已跌破{worst.get('label', '止损线')} "
                f"{worst['price']:.2f}"
            )
            if worst.get("reason"):
                msg += f" — {worst['reason']}"
            items.append({
                "code": code, "name": display,
                "type": "stop_loss_breach", "severity": "urgent",
                "message": msg,
            })

    # 2. sell verdict on a held stock.
    if analysis is not None and kt.get("actionable") == "建议卖出":
        age = _age_days(analysis.created_at)
        reason = kt.get("one_line_reason") or ""
        msg = f"AI 建议卖出：{reason}".rstrip("：")
        if age >= 1:
            msg += f"（解析生成于 {age} 天前）"
        items.append({
            "code": code, "name": display,
            "type": "sell_verdict", "severity": "urgent",
            "message": msg,
        })
    # 2b. 持仓象限立场看空(sell_stance)。actionable 混了两个受众——
    # 不建议入手 是说给买家的,持仓者的动作在 scenario_advice 里;这里把
    # 用户象限的看空立场提上来。actionable 已是 建议卖出 时不重复报
    # (同一个意图);severity=warn,保持 sell_verdict(urgent)的信号层级。
    elif analysis is not None:
        sdir = kt.get("scenario_direction") or {}
        if isinstance(sdir, dict) and sdir.get(quadrant) == "看空":
            sadv = kt.get("scenario_advice") or {}
            advice = sadv.get(quadrant) if isinstance(sadv, dict) else None
            age = _age_days(analysis.created_at)
            msg = f"AI 对持仓者立场看空：{advice or '建议关注减仓时机'}"
            if age >= 1:
                msg += f"（解析生成于 {age} 天前）"
            items.append({
                "code": code, "name": display,
                "type": "sell_stance", "severity": "warn",
                "message": msg,
            })

    # 3. validity window lapsed.
    if analysis is not None and kt.get("valid_window"):
        expired, why = check_valid_window(
            str(kt["valid_window"]), analysis.created_at, price, now=now,
        )
        if expired:
            items.append({
                "code": code, "name": display,
                "type": "valid_window_expired", "severity": "warn",
                "message": (
                    f"建议有效期已过（{kt['valid_window']}；{why}）"
                    f"，当前建议「{kt.get('actionable', '?')}」可能已失效，建议重新解析"
                ),
            })

    # 4. new strong signal vs the analysis's anchor snapshot. Without an
    # anchor (analysis missing / pre-snapshot_id rows) we skip rather than
    # alarm on every long-standing signal.
    if snap is not None and anchor_snap is not None:
        new_strong = (
            (set(snap.signals or []) - set(anchor_snap.signals or []))
            & STRONG_SIGNALS
        )
        for sig in sorted(new_strong):
            items.append({
                "code": code, "name": display,
                "type": "signal_alert",
                "severity": "urgent" if sig in BEARISH_STRONG else "warn",
                "message": f"解析生成后出现新强信号：{sig}（当前建议未纳入该信息，建议重新解析）",
            })

    return items


# ---------------------------------------------------------------------------
# DB orchestration
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = {"urgent": 0, "warn": 1}
_TYPE_ORDER = {
    "stop_loss_breach": 0, "sell_verdict": 1, "sell_stance": 2,
    "signal_alert": 3, "valid_window_expired": 4,
}

_PNL_TO_QUADRANT = {"盈": "holding_big_gain", "亏": "holding_big_loss"}


def compute_for_user(db: Session, owner_id: int | None) -> dict[str, Any]:
    """Action items across the user's **default-held** universe: 自选列表
    ∪ 录了成本价的 Holding,减去漏斗里显式标了未持仓的票。Cheap: one query
    per table, evaluation is in-memory."""
    holdings = {
        h.code: h for h in
        db.query(Holding).filter(Holding.user_id == owner_id).all()
    }
    wq = db.query(Watchlist)
    if owner_id is not None:
        wq = wq.filter(Watchlist.user_id == owner_id)
    watch_rows = wq.all()

    # Latest funnel choice per code (append-only table → newest row wins).
    # owner_id None(legacy no-auth)时查不到属于谁的选择,全按默认持仓。
    latest_funnel: dict[str, FunnelChoice] = {}
    if owner_id is not None:
        fq = (
            db.query(FunnelChoice)
            .filter(FunnelChoice.user_id == owner_id)
            .order_by(desc(FunnelChoice.created_at), desc(FunnelChoice.id))
        )
        for fc in fq:
            latest_funnel.setdefault(fc.code, fc)

    codes = set(w.code for w in watch_rows) | set(holdings.keys())
    # 显式标了未持仓的票出列;录了成本价的 Holding 视为持有(覆盖旧漏斗标记)。
    codes = {
        c for c in codes
        if c in holdings
        or c not in latest_funnel
        or latest_funnel[c].held
    }
    if not codes:
        return {"items": [], "checked_holdings": 0}

    # Names: prefer the user's own watchlist rows; fall back to any user's
    # row for the code (names are market facts, not per-user state).
    names: dict[str, str] = {}
    for w in db.query(Watchlist).filter(Watchlist.code.in_(list(codes))).all():
        if w.user_id == owner_id or w.code not in names:
            names[w.code] = w.name

    analyses = {
        a.code: a for a in
        db.query(Analysis).filter(Analysis.code.in_(list(codes))).all()
    }

    # Latest snapshot per code + anchor snapshots referenced by analyses.
    latest: dict[str, Snapshot] = {}
    for code in codes:
        s = (
            db.query(Snapshot)
            .filter(Snapshot.code == code)
            .order_by(desc(Snapshot.id))
            .first()
        )
        if s is not None:
            latest[code] = s
    anchor_ids = [
        a.snapshot_id for a in analyses.values() if a.snapshot_id is not None
    ]
    anchors_by_id = {
        s.id: s for s in
        (db.query(Snapshot).filter(Snapshot.id.in_(anchor_ids)).all()
         if anchor_ids else [])
    }

    def _quadrant(code: str) -> str:
        """用户所在持仓象限:成本价可算 → 按浮盈亏(±10% = 大幅,同前端
        pnlBucketFromPct);漏斗标过盈亏档 → 按标的;都没有 → 小幅波动
        (盈亏不构成决策因素的默认格)。"""
        h = holdings.get(code)
        snap = latest.get(code)
        price = snap.price if snap is not None else None
        if h is not None and h.cost_price and price is not None:
            pct = (price - h.cost_price) / h.cost_price * 100
            if pct >= 10:
                return "holding_big_gain"
            if pct <= -10:
                return "holding_big_loss"
            return "holding_small"
        fc = latest_funnel.get(code)
        if fc is not None and fc.held and fc.pnl:
            return _PNL_TO_QUADRANT.get(fc.pnl, "holding_small")
        return "holding_small"

    items: list[dict[str, Any]] = []
    for code in sorted(codes):
        a = analyses.get(code)
        anchor = anchors_by_id.get(a.snapshot_id) if a is not None else None
        items.extend(evaluate_code(
            code, names.get(code), latest.get(code), a, anchor,
            quadrant=_quadrant(code),
        ))

    items.sort(key=lambda it: (
        _SEVERITY_ORDER.get(it["severity"], 9),
        _TYPE_ORDER.get(it["type"], 9),
        it["code"],
    ))
    return {"items": items, "checked_holdings": len(codes)}
