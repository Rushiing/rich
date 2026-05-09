"""Sector (板块) ranking — sina sector spot endpoint.

`stock_sector_spot('新浪行业')` returns 49 sectors in one call (~1s).
Cached in-process for 5 min so the /sectors page doesn't re-pull on every
viewer. Returned shape is the row dict the route serializes directly.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import akshare as ak

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 5 * 60   # 5 min — sector summary moves slowly intra-day

_cache: list[dict[str, Any]] = []
_cache_loaded_at: float = 0.0
_lock = threading.RLock()


def _strip_sina_code_prefix(s: str) -> str:
    """sina returns leading stock as 'sh600519' / 'sz000001' — strip the
    2-letter exchange prefix so the frontend can link to /stocks/{code}."""
    s = (s or "").strip()
    if len(s) == 8 and s[:2] in ("sh", "sz", "bj"):
        return s[2:]
    return s


def _refresh() -> None:
    global _cache_loaded_at
    try:
        df = ak.stock_sector_spot("新浪行业")
    except Exception:
        logger.exception("sectors: akshare fetch failed; keeping prior cache")
        return
    if df is None or len(df) == 0:
        return

    rows: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        try:
            rows.append({
                "name": str(r.get("板块") or "").strip(),
                "code": str(r.get("label") or "").strip(),
                "company_count": int(r.get("公司家数") or 0),
                "avg_price": float(r.get("平均价格") or 0) or None,
                "change_pct": float(r.get("涨跌幅") or 0),
                "total_volume": float(r.get("总成交量") or 0) or None,
                "total_turnover": float(r.get("总成交额") or 0) or None,
                "leader": {
                    "code": _strip_sina_code_prefix(str(r.get("股票代码") or "")),
                    "name": str(r.get("股票名称") or "").strip(),
                    "change_pct": float(r.get("个股-涨跌幅") or 0),
                    "price": float(r.get("个股-当前价") or 0) or None,
                },
            })
        except Exception:
            logger.exception("sectors: row parse failed for %r", r.get("板块"))
            continue

    # Sort by sector change_pct desc so the "hottest sector today" is on top
    rows.sort(key=lambda x: x.get("change_pct") or 0, reverse=True)
    with _lock:
        _cache.clear()
        _cache.extend(rows)
        _cache_loaded_at = time.time()
    logger.info("sectors: cached %d sectors", len(rows))


def get_sectors() -> list[dict[str, Any]]:
    """Returns the cached sector list, refreshing if stale."""
    now = time.time()
    if not _cache or (now - _cache_loaded_at) > CACHE_TTL_SECONDS:
        _refresh()
    with _lock:
        return list(_cache)
