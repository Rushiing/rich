"""Realtime A-share quotes via sina hq direct HTTP.

Why this exists: Railway egress (even from the Singapore region) can't
reliably reach push2.eastmoney.com or xueqiu.com — both akshare paths
that bulk_em / per-code spot rely on come back as RemoteDisconnected
or 'data' KeyError. Sina's hq.sinajs.cn host has historically been more
forgiving toward overseas IPs and returns plain CSV-ish text we can
parse without any third-party library.

Used as the primary path in services.scraper.collect_quotes_bulk; akshare
stays as a fallback for the rare cases sina doesn't cover.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

SINA_BASE = "https://hq.sinajs.cn/list="
# Sina rejects requests without a finance.sina.com.cn referer (returns 412
# with an "Bad Request" body). The user-agent is also checked loosely.
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0",
}

# Response is GBK; one var per code.
_LINE_RE = re.compile(r'hq_str_(\w+)="([^"]*)"')


def _sina_symbol(code: str) -> str:
    if code.startswith(("60", "68")):
        return "sh" + code
    if code.startswith(("00", "30")):
        return "sz" + code
    if code.startswith(("8", "4")):
        return "bj" + code
    return code  # let sina reject if it doesn't recognize


def fetch_quotes_sina(
    codes: list[str], chunk: int = 50, timeout: int = 8
) -> dict[str, dict[str, Any]]:
    """Pull realtime spot quotes for a batch of codes from sina hq.

    Returns {code: {price, change_pct, volume, turnover}} for codes sina
    returned a non-empty payload for. Codes sina didn't cover (delisted /
    BJ stocks sometimes / unknown) are simply absent.

    Sina returns a *single* response for the whole batch, so a watchlist
    of 30 codes is one HTTP round-trip — vastly cheaper and more reliable
    than akshare's per-code fan-out, which is why this is the primary
    path on Railway.
    """
    if not codes:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(codes), chunk):
        batch = codes[i:i + chunk]
        url = SINA_BASE + ",".join(_sina_symbol(c) for c in batch)
        try:
            req = Request(url, headers=SINA_HEADERS)
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("gbk", errors="replace")
        except (URLError, TimeoutError) as e:
            logger.warning("sina quotes fetch failed for %d codes: %s", len(batch), e)
            continue
        for sym, payload in _LINE_RE.findall(body):
            # Empty quote ("") = sina has nothing for this symbol (delisted,
            # unsupported board, or temporary outage).
            if not payload.strip():
                continue
            code = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
            parsed = _parse_sina_row(payload)
            if parsed is not None:
                out[code] = parsed
    return out


def _parse_sina_row(payload: str) -> dict[str, Any] | None:
    """Sina A-share row layout (comma-separated):
       0: name, 1: today open, 2: prev close, 3: current price,
       4: high, 5: low, 6: bid1, 7: ask1, 8: volume(股), 9: turnover(元),
       10..29: order book, 30: date, 31: time

    We only need price / change_pct / volume / turnover.
    """
    fields = payload.split(",")
    if len(fields) < 10:
        return None
    try:
        prev_close = float(fields[2])
        price = float(fields[3])
        volume = float(fields[8])
        turnover = float(fields[9])
    except (ValueError, IndexError):
        return None
    if price <= 0:
        # Halted / no trades today — sina returns 0 for current price in
        # this state, which would render as a -100% change downstream.
        return None
    change_pct = ((price - prev_close) / prev_close * 100.0) if prev_close > 0 else None
    return {
        "price": price,
        "change_pct": change_pct,
        "volume": volume if volume > 0 else None,
        "turnover": turnover if turnover > 0 else None,
    }
