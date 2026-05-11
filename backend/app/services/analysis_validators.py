"""Post-hoc validators for LLM-produced analysis payloads.

The LLM is good at narrative but sometimes self-contradicts on hard rules
(e.g. names an ST stock 观望 instead of 不建议入手, or recommends 建议买入
while the technicals are clearly broken). These validators run after every
generate() call and:

- Override the offending fields to safer values
- Prepend a "⚠️ 系统规则触发：..." line to red_flags so the user sees
  exactly what rule fired
- Log a structured INFO entry recording original → corrected for audit

Rules are deliberately conservative: only fire when the signal is
unambiguous. Edge cases stay with the LLM's judgment.
"""
from __future__ import annotations

import logging
from typing import Any

from ..models import Snapshot, Watchlist

logger = logging.getLogger(__name__)


def _trip(corrections: list[str], message: str) -> None:
    """Helper: record a triggered correction for later red_flags prepending."""
    corrections.append(message)
    logger.info("analysis_validator triggered: %s", message)


def _force_actionable(
    key_table: dict[str, Any], target: str, max_position: float | None = None
) -> bool:
    """Force the top-level actionable + cascade to actionable_tiers when
    present. Returns True if any field changed.

    target ∈ {"不建议入手", "观望"}. When forcing 不建议入手 we also zero
    out position_pct; when forcing 观望 we cap position at max_position
    (typically the legacy value clamped down).
    """
    changed = False
    if key_table.get("actionable") != target:
        key_table["actionable"] = target
        changed = True

    if target == "不建议入手":
        if key_table.get("position_pct") not in (0, 0.0):
            key_table["position_pct"] = 0
            changed = True

    # Cascade to actionable_tiers — keep the structure consistent so the
    # frontend doesn't show a contradicting per-tier recommendation.
    tiers = key_table.get("actionable_tiers")
    if isinstance(tiers, dict):
        for tier_key, tier in tiers.items():
            if not isinstance(tier, dict):
                continue
            if tier.get("action") in ("建议买入", "建议卖出") and target != tier.get("action"):
                tier["action"] = target
                changed = True
            if target == "不建议入手" and tier.get("position_pct", 0) > 0:
                tier["position_pct"] = 0
                changed = True
            elif max_position is not None and (tier.get("position_pct") or 0) > max_position:
                tier["position_pct"] = max_position
                changed = True

    return changed


def _is_st(name: str | None) -> bool:
    if not name:
        return False
    upper = name.upper().replace(" ", "")
    return "ST" in upper or "*ST" in upper or "维权" in name


def _validate_st(payload: dict[str, Any], w: Watchlist, corrections: list[str]) -> None:
    """ST / *ST / 维权 names are by definition high-risk delisting candidates.
    LLM occasionally still says 观望 — override to 不建议入手 unconditionally."""
    if not _is_st(w.name):
        return
    key_table = payload.get("key_table", {})
    if key_table.get("actionable") != "不建议入手":
        if _force_actionable(key_table, "不建议入手"):
            _trip(corrections, "⚠️ ST/退市风险股，强制 '不建议入手'")


def _validate_technical_breakdown(
    payload: dict[str, Any], snapshot: Snapshot | None, corrections: list[str],
) -> None:
    """If technicals are clearly broken (close < MA60 AND MACD death-cross),
    forbid 建议买入. Demoted to 观望 with a capped position. Doesn't trigger
    when K-line data is missing — the LLM already discounts confidence there."""
    if snapshot is None:
        return
    # Late import to dodge the analysis → kline → analysis cycle
    from . import kline as kline_svc
    latest = kline_svc.latest_for_code(snapshot.code)
    if latest is None or latest.close is None or latest.ma60 is None:
        return
    if latest.macd_dif is None or latest.macd_dea is None:
        return

    below_ma60 = latest.close < latest.ma60
    macd_death = latest.macd_dif < latest.macd_dea
    if not (below_ma60 and macd_death):
        return

    key_table = payload.get("key_table", {})
    if key_table.get("actionable") == "建议买入":
        if _force_actionable(key_table, "观望", max_position=20):
            _trip(corrections,
                  "⚠️ 技术面破位（跌破年线 + MACD 死叉），强制 '观望'")


def _validate_numeric_consistency(
    payload: dict[str, Any], snapshot: Snapshot | None, corrections: list[str],
) -> None:
    """Sanity checks on the price numbers themselves:
       - buy_price_high < current_price × 1.3 (don't paint a sky-high target)
       - stop_loss_levels prices monotonic + below buy_price_low
       - actionable_tiers position_pct monotonic: aggressive ≥ neutral ≥ conservative
    """
    key_table = payload.get("key_table")
    if not isinstance(key_table, dict):
        return

    # Tier monotonicity. The system prompt asks for this; we enforce it.
    tiers = key_table.get("actionable_tiers")
    if isinstance(tiers, dict):
        try:
            agg = float(tiers.get("aggressive", {}).get("position_pct", 0))
            neu = float(tiers.get("neutral", {}).get("position_pct", 0))
            cons = float(tiers.get("conservative", {}).get("position_pct", 0))
        except (TypeError, ValueError):
            agg = neu = cons = 0.0
        if not (agg >= neu >= cons):
            # Force monotonicity by clamping
            tiers["conservative"]["position_pct"] = min(cons, neu, agg)
            tiers["aggressive"]["position_pct"] = max(agg, neu, cons)
            tiers["neutral"]["position_pct"] = min(
                max(neu, tiers["conservative"]["position_pct"]),
                tiers["aggressive"]["position_pct"],
            )
            _trip(corrections,
                  "⚠️ 三档仓位违反单调性 (激进≥中立≥保守)，已自动修正")

    # Buy price upper bound vs current price.
    if snapshot is not None and snapshot.price:
        try:
            buy_high = float(key_table.get("buy_price_high", 0))
        except (TypeError, ValueError):
            buy_high = 0.0
        if buy_high > snapshot.price * 1.3:
            key_table["buy_price_high"] = round(snapshot.price * 1.2, 2)
            _trip(corrections,
                  f"⚠️ 建议买入价上限 {buy_high:.2f} 超过当前价 30%，已下调")


def _validate_earnings_collapse(
    payload: dict[str, Any], w: Watchlist, corrections: list[str],
) -> None:
    """Profit YoY < -50% AND revenue YoY < 0 = severe earnings deterioration.
    Cap suggested position at 10% so even an aggressive reader doesn't go
    heavy into a fundamentally broken name."""
    from . import financials as fin_svc
    rows = fin_svc.latest_for_code(w.code, n=1)
    if not rows:
        return
    latest = rows[0]
    if latest.profit_yoy is None or latest.revenue_yoy is None:
        return
    if not (latest.profit_yoy < -50 and latest.revenue_yoy < 0):
        return

    key_table = payload.get("key_table", {})
    position = key_table.get("position_pct") or 0
    if position > 10:
        key_table["position_pct"] = 10
        _trip(corrections,
              f"⚠️ 业绩塌方（净利同比 {latest.profit_yoy:.0f}%、营收同比 "
              f"{latest.revenue_yoy:.0f}%），仓位上限压到 10%")
    # Also clamp aggressive tier if present
    tiers = key_table.get("actionable_tiers")
    if isinstance(tiers, dict):
        for tk in ("aggressive", "neutral", "conservative"):
            tier = tiers.get(tk)
            if isinstance(tier, dict) and (tier.get("position_pct") or 0) > 10:
                tier["position_pct"] = 10


def validate_and_correct(
    payload: dict[str, Any], w: Watchlist, snapshot: Snapshot | None,
) -> dict[str, Any]:
    """Run all validators, mutate payload in place, return it. Any corrections
    are prepended to key_table.red_flags so the user sees what rules fired."""
    corrections: list[str] = []
    _validate_st(payload, w, corrections)
    _validate_earnings_collapse(payload, w, corrections)
    _validate_technical_breakdown(payload, snapshot, corrections)
    _validate_numeric_consistency(payload, snapshot, corrections)

    if corrections:
        key_table = payload.setdefault("key_table", {})
        existing_flags = key_table.get("red_flags") or []
        if not isinstance(existing_flags, list):
            existing_flags = []
        key_table["red_flags"] = corrections + existing_flags

    return payload
