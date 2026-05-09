"""3-day rolling metrics — bulk pull from akshare's 3日排行 endpoint.

The full A-share market (~5180 rows) comes back in a single call (~20s),
covering 阶段涨跌幅 (3-day change %), 连续换手率 (cumulative 3-day turnover
%), and 资金流入净额 (3-day net main-force flow in 元). Pulling once per
hour and caching in-memory keeps every snapshot/quote tick cheap (dict
lookup) without re-paginating.

Output unit normalization: percentages stripped of "%" and turned into
floats; flow strings like "1.78亿" / "2491.80万" / "-2.80亿" parsed into
yuan as floats.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import akshare as ak

logger = logging.getLogger(__name__)

# Refresh cadence — 3-day metrics update once a day at most, but we want a
# safety margin for the case where akshare's first call lands stale.
CACHE_TTL_SECONDS = 30 * 60   # 30 minutes

_cache: dict[str, dict[str, float]] = {}  # code → {change_pct_3d, ...}
_cache_loaded_at: float = 0.0
_lock = threading.RLock()


_PCT_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _parse_pct(s: Any) -> float | None:
    """'58.75%' → 58.75; None / '-' / unparseable → None."""
    if s is None:
        return None
    s = str(s).strip().rstrip("%").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_flow(s: Any) -> float | None:
    """'1.78亿' → 178_000_000.0; '2491.80万' → 24_918_000.0; '-2.80亿' → -2.8e8."""
    if s is None:
        return None
    raw = str(s).strip()
    if not raw or raw == "-":
        return None
    sign = 1.0
    if raw.startswith("-"):
        sign = -1.0
        raw = raw[1:]
    multiplier = 1.0
    if raw.endswith("亿"):
        multiplier = 1e8
        raw = raw[:-1]
    elif raw.endswith("万"):
        multiplier = 1e4
        raw = raw[:-1]
    try:
        return sign * float(raw) * multiplier
    except ValueError:
        return None


def _refresh() -> None:
    """Pull the full 3-day rank table and overwrite the cache. Logs but
    does not raise on akshare failure — callers fall back to whatever the
    previous cache had (or DB aggregation upstream)."""
    global _cache_loaded_at
    try:
        df = ak.stock_fund_flow_individual("3日排行")
    except Exception:
        logger.exception("three_day: akshare fetch failed; keeping prior cache")
        return
    if df is None or len(df) == 0:
        logger.warning("three_day: empty response, skipping cache update")
        return

    fresh: dict[str, dict[str, float]] = {}
    for _, row in df.iterrows():
        code = str(row.get("股票代码") or "").strip()
        if not re.match(r"^\d{6}$", code):
            continue
        chg = _parse_pct(row.get("阶段涨跌幅"))
        turn = _parse_pct(row.get("连续换手率"))
        flow = _parse_flow(row.get("资金流入净额"))
        if chg is None and turn is None and flow is None:
            continue
        fresh[code] = {
            "change_pct_3d": chg,
            "turnover_rate_3d": turn,
            "net_flow_3d": flow,
        }
    with _lock:
        _cache.clear()
        _cache.update(fresh)
        _cache_loaded_at = time.time()
    logger.info("three_day: cached %d codes", len(fresh))


def get_metrics(codes: list[str]) -> dict[str, dict[str, float]]:
    """Returns {code: {change_pct_3d, turnover_rate_3d, net_flow_3d}} for
    codes the cache covers. Auto-refreshes the cache when older than the
    TTL. Codes not in akshare's rank list (very rare — endpoint covers
    full market) are simply absent; caller should fall back to DB
    aggregation in services/aggregates.py."""
    now = time.time()
    if not _cache or (now - _cache_loaded_at) > CACHE_TTL_SECONDS:
        _refresh()
    with _lock:
        return {c: _cache[c] for c in codes if c in _cache}
