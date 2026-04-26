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

# Threshold for "main-force big inflow" signal — ¥50,000,000.
BIG_INFLOW_YUAN = 50_000_000.0


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


# (name, predicate, is_strong)
RULES: list[tuple[str, Callable[[dict[str, Any]], bool], bool]] = [
    ("limit_up", _is_limit_up, True),
    ("limit_down", _is_limit_down, True),
    ("big_inflow", _big_inflow, False),
    ("big_outflow", _big_outflow, False),
    ("important_notice", _important_notice, True),
    ("lhb", _on_lhb, True),
]

STRONG_SIGNALS = {name for name, _, strong in RULES if strong}


def compute_signals(snap: dict[str, Any]) -> list[str]:
    return [name for name, pred, _ in RULES if pred(snap)]


def has_strong(signals: list[str]) -> bool:
    return any(s in STRONG_SIGNALS for s in signals)
