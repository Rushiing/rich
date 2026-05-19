"""Market-wide index quotes for the dashboard.

Pulls 上证指数 / 深证成指 / 创业板指 from Tencent qt.gtimg.cn — the same
host our realtime stock quotes use (Railway-reachable). Index symbols are
hardcoded because the stock code→exchange mapping doesn't apply to
indices (e.g. index 000001 is 上证 = sh, but stock 000001 is sz).

Cached 60s in-process — index points move slowly enough intra-day.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# (tencent symbol, display name). Order = display order on the dashboard.
INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
]

TENCENT_BASE = "https://qt.gtimg.cn/q="
_LINE_RE = re.compile(r'v_(\w+)="([^"]*)"')

CACHE_TTL_SECONDS = 60

_cache: list[dict[str, Any]] = []
_cache_loaded_at: float = 0.0
_lock = threading.RLock()


def _refresh() -> None:
    global _cache_loaded_at
    url = TENCENT_BASE + ",".join(sym for sym, _ in INDICES)
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as resp:
            body = resp.read().decode("gbk", errors="replace")
    except (URLError, TimeoutError) as e:
        logger.warning("market indices fetch failed: %s", e)
        return

    parsed: dict[str, dict[str, Any]] = {}
    for sym, payload in _LINE_RE.findall(body):
        fields = payload.split("~")
        if len(fields) < 33:
            continue
        try:
            point = float(fields[3])          # current index point
            change_pct = float(fields[32])    # 涨跌幅 %
        except (ValueError, IndexError):
            continue
        parsed[sym] = {"point": point, "change_pct": change_pct}

    rows: list[dict[str, Any]] = []
    for sym, name in INDICES:
        d = parsed.get(sym)
        if d is None:
            continue
        rows.append({"symbol": sym, "name": name,
                     "point": d["point"], "change_pct": d["change_pct"]})
    if rows:
        with _lock:
            _cache.clear()
            _cache.extend(rows)
            _cache_loaded_at = time.time()


def get_indices() -> list[dict[str, Any]]:
    """Cached index list; refreshes when stale."""
    now = time.time()
    if not _cache or (now - _cache_loaded_at) > CACHE_TTL_SECONDS:
        _refresh()
    with _lock:
        return list(_cache)
