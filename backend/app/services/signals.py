"""Signal computation — pure functions over snapshot dicts.

Strong signals are flagged for red-row highlighting in the 盯盘 view.
Add new rules by appending to RULES; each is `(name, predicate, is_strong)`.
"""
from __future__ import annotations

from typing import Any, Callable

LIMIT_UP_PCT_BY_PREFIX = {
    "30": 20.0,  # 创业板
    "68": 20.0,  # 科创板
    "60": 10.0,  # 沪市主板
    "00": 10.0,  # 深市主板/中小
    "8": 30.0,   # 北交所 (approximate)
    "4": 30.0,
}

# Threshold for "main-force big inflow" signal — ¥20,000,000.
# Lowered from ¥50M after a 4/27 review: too many real mid-cap moves
# (¥20–50M flow on a +3–7% day) were falling off the signal radar.
BIG_INFLOW_YUAN = 20_000_000.0


def _limit_pct(code: str) -> float:
    for prefix, pct in LIMIT_UP_PCT_BY_PREFIX.items():
        if code.startswith(prefix):
            return pct
    return 10.0


def _is_limit_up(snap: dict[str, Any]) -> bool:
    pct = snap.get("change_pct")
    if pct is None:
        return False
    return pct >= _limit_pct(snap["code"]) - 0.5  # within 0.5% of the cap


def _is_limit_down(snap: dict[str, Any]) -> bool:
    pct = snap.get("change_pct")
    if pct is None:
        return False
    return pct <= -(_limit_pct(snap["code"]) - 0.5)


def _big_inflow(snap: dict[str, Any]) -> bool:
    flow = snap.get("main_net_flow")
    return flow is not None and flow >= BIG_INFLOW_YUAN


def _big_outflow(snap: dict[str, Any]) -> bool:
    flow = snap.get("main_net_flow")
    return flow is not None and flow <= -BIG_INFLOW_YUAN


def _important_notice(snap: dict[str, Any]) -> bool:
    return any(n.get("type") for n in (snap.get("notices") or []))


def _on_lhb(snap: dict[str, Any]) -> bool:
    return bool(snap.get("lhb"))


# Phase 9: technical signals derived from K-line + indicators. The cron
# attaches a `kline` dict (from services/kline.latest_for_code) onto the
# snap dict before compute_signals runs; predicates are no-ops when it's
# absent so non-Phase-9 paths keep working.

def _kline(snap: dict[str, Any]) -> dict | None:
    k = snap.get("kline")
    return k if isinstance(k, dict) else None


def _breakout_20d(snap: dict[str, Any]) -> bool:
    """Today's close is a fresh 20-day high. Strong bullish signal."""
    k = _kline(snap)
    if k is None:
        return False
    close = k.get("close")
    high20 = k.get("high20")  # cron precomputes; falls back to MA20 envelope below
    if close is None or high20 is None:
        return False
    return float(close) >= float(high20) - 1e-9


def _below_ma60(snap: dict[str, Any]) -> bool:
    """Close drops below MA60 (年线) — bearish trend break."""
    k = _kline(snap)
    if k is None:
        return False
    close, ma60 = k.get("close"), k.get("ma60")
    if close is None or ma60 is None:
        return False
    return float(close) < float(ma60)


def _macd_golden_cross(snap: dict[str, Any]) -> bool:
    k = _kline(snap)
    if k is None:
        return False
    dif, dea, prev_dif, prev_dea = (
        k.get("macd_dif"), k.get("macd_dea"),
        k.get("macd_dif_prev"), k.get("macd_dea_prev"),
    )
    if any(v is None for v in (dif, dea, prev_dif, prev_dea)):
        return False
    return prev_dif <= prev_dea and dif > dea


def _macd_death_cross(snap: dict[str, Any]) -> bool:
    k = _kline(snap)
    if k is None:
        return False
    dif, dea, prev_dif, prev_dea = (
        k.get("macd_dif"), k.get("macd_dea"),
        k.get("macd_dif_prev"), k.get("macd_dea_prev"),
    )
    if any(v is None for v in (dif, dea, prev_dif, prev_dea)):
        return False
    return prev_dif >= prev_dea and dif < dea


# (name, predicate, is_strong)
RULES: list[tuple[str, Callable[[dict[str, Any]], bool], bool]] = [
    ("limit_up", _is_limit_up, True),
    ("limit_down", _is_limit_down, True),
    ("big_inflow", _big_inflow, False),
    ("big_outflow", _big_outflow, False),
    ("important_notice", _important_notice, True),
    ("lhb", _on_lhb, True),
    # Phase 9 technicals
    ("breakout_20d", _breakout_20d, True),
    ("below_ma60", _below_ma60, True),
    ("macd_golden_cross", _macd_golden_cross, False),
    ("macd_death_cross", _macd_death_cross, False),
]

STRONG_SIGNALS = {name for name, _, strong in RULES if strong}


def compute_signals(snap: dict[str, Any]) -> list[str]:
    return [name for name, pred, _ in RULES if pred(snap)]


def has_strong(signals: list[str]) -> bool:
    return any(s in STRONG_SIGNALS for s in signals)
