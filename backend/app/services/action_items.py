"""今日需行动 — 持仓感知的卖出触发 (S1).

For each holding of the requesting user, check the (globally shared)
latest snapshot + cached analysis for conditions that mean "今天该看一眼
这支票了":

1. stop_loss_breach — current price at/below any of the analysis's
   stop_loss_levels. The single most direct "act now" signal.
2. sell_verdict — the cached analysis verdict is 建议卖出 on a stock the
   user actually holds.
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
evaluate_holding() takes plain rows, compute_for_user() does the queries.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..models import Analysis, Holding, Snapshot, Watchlist
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
# Per-holding evaluation (pure)
# ---------------------------------------------------------------------------

def evaluate_holding(
    holding: Holding,
    name: str | None,
    snap: Snapshot | None,
    analysis: Analysis | None,
    anchor_snap: Snapshot | None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Run the four checks for one holding. Returns 0..n action items:
    {code, name, type, severity, message}. severity ∈ {urgent, warn}."""
    items: list[dict[str, Any]] = []
    code = holding.code
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
    "stop_loss_breach": 0, "sell_verdict": 1,
    "signal_alert": 2, "valid_window_expired": 3,
}


def compute_for_user(db: Session, owner_id: int | None) -> dict[str, Any]:
    """Action items across all holdings of `owner_id`. Cheap: one query
    per table, evaluation is in-memory."""
    holdings = db.query(Holding).filter(Holding.user_id == owner_id).all()
    if not holdings:
        return {"items": [], "checked_holdings": 0}

    codes = [h.code for h in holdings]

    # Names: prefer the user's own watchlist rows; fall back to any user's
    # row for the code (names are market facts, not per-user state).
    names: dict[str, str] = {}
    for w in db.query(Watchlist).filter(Watchlist.code.in_(codes)).all():
        if w.user_id == owner_id or w.code not in names:
            names[w.code] = w.name

    analyses = {
        a.code: a for a in
        db.query(Analysis).filter(Analysis.code.in_(codes)).all()
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

    items: list[dict[str, Any]] = []
    for h in holdings:
        a = analyses.get(h.code)
        anchor = anchors_by_id.get(a.snapshot_id) if a is not None else None
        items.extend(evaluate_holding(
            h, names.get(h.code), latest.get(h.code), a, anchor,
        ))

    items.sort(key=lambda it: (
        _SEVERITY_ORDER.get(it["severity"], 9),
        _TYPE_ORDER.get(it["type"], 9),
        it["code"],
    ))
    return {"items": items, "checked_holdings": len(holdings)}
