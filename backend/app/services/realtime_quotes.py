"""Realtime A-share quotes via direct HTTP — sina hq + Tencent qt.gtimg.cn.

Why this exists: Railway egress (even from the Singapore region) can't
reliably reach push2.eastmoney.com or xueqiu.com — both akshare paths
that bulk_em / per-code spot rely on come back as RemoteDisconnected
or 'data' KeyError. Sina's hq.sinajs.cn and Tencent's qt.gtimg.cn are
both forgiving toward overseas IPs and return plain CSV-ish text we
can parse without any third-party library.

Tencent is preferred when reachable because its payload carries
valuation metrics (PE / PB / 换手率 / 总市值 / 流通市值) that sina doesn't.
Sina remains a fallback for codes Tencent missed and as a backup if
Tencent is down. Used together by services.scraper.collect_quotes_bulk;
akshare stays as a deeper fallback.
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


# --- Tencent qt.gtimg.cn ---------------------------------------------------
# Same one-HTTP-roundtrip-per-batch shape as sina, but the response carries
# valuation metrics (PE/PB/换手率/总市值/流通市值) that sina doesn't.

TENCENT_BASE = "https://qt.gtimg.cn/q="
_TENCENT_LINE_RE = re.compile(r'v_(\w+)="([^"]*)"')


def _tencent_symbol(code: str) -> str:
    if code.startswith(("60", "68")):
        return "sh" + code
    if code.startswith(("00", "30")):
        return "sz" + code
    if code.startswith(("8", "4")):
        return "bj" + code
    return code


def fetch_names_tencent(
    codes: list[str], chunk: int = 50, timeout: int = 8
) -> dict[str, str]:
    """Pull stock 简称 for a batch of codes from qt.gtimg.cn. One HTTP call
    per chunk-of-50, returns {code: name}. Used by services.stocks.lookup_codes
    as the primary watchlist-import path because eastmoney's per-stock info
    endpoint is blocked on Railway.

    Codes Tencent doesn't recognize (delisted / wrong format) are simply
    absent from the result — caller decides how to surface that to the UI.
    """
    if not codes:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(codes), chunk):
        batch = codes[i:i + chunk]
        url = TENCENT_BASE + ",".join(_tencent_symbol(c) for c in batch)
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("gbk", errors="replace")
        except (URLError, TimeoutError) as e:
            logger.warning("tencent name fetch failed for %d codes: %s", len(batch), e)
            continue
        for sym, payload in _TENCENT_LINE_RE.findall(body):
            if not payload.strip():
                continue
            code = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
            fields = payload.split("~")
            if len(fields) < 3:
                continue
            name = (fields[1] or "").strip()
            if name:
                out[code] = name
    return out


def fetch_quotes_tencent(
    codes: list[str], chunk: int = 50, timeout: int = 8
) -> dict[str, dict[str, Any]]:
    """Pull realtime quotes from Tencent's qt.gtimg.cn.

    Returns {code: {price, change_pct, volume, turnover, turnover_rate,
    pe_ratio, pb_ratio, market_cap, circ_market_cap}} for codes Tencent
    returned a non-empty payload for. Codes Tencent didn't cover are absent;
    caller should fall back to sina + akshare.
    """
    if not codes:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(codes), chunk):
        batch = codes[i:i + chunk]
        url = TENCENT_BASE + ",".join(_tencent_symbol(c) for c in batch)
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("gbk", errors="replace")
        except (URLError, TimeoutError) as e:
            logger.warning("tencent quotes fetch failed for %d codes: %s", len(batch), e)
            continue
        for sym, payload in _TENCENT_LINE_RE.findall(body):
            if not payload.strip():
                continue
            code = sym[2:] if sym[:2] in ("sh", "sz", "bj") else sym
            parsed = _parse_tencent_row(payload)
            if parsed is not None:
                out[code] = parsed
    return out


def _parse_tencent_row(payload: str) -> dict[str, Any] | None:
    """Tencent A-share row layout (tilde-separated). Field positions verified
    against live qt.gtimg.cn responses (4/27):

        3:  current price
        4:  prev close
        6:  volume (手 = lots of 100 shares)
        32: change %
        37: turnover (万元)
        38: 换手率 (%)
        39: 市盈率（动态）
        44: 流通市值 (亿元)
        45: 总市值 (亿元)
        46: 市净率

    Returns None if the row is unusable. Volume is normalized to *shares* to
    align with the sina parser; turnover and market caps to *元*.
    """
    fields = payload.split("~")
    if len(fields) < 47:
        return None

    def _f(idx: int) -> float | None:
        try:
            v = fields[idx].strip()
            return float(v) if v else None
        except (ValueError, IndexError):
            return None

    price = _f(3)
    prev_close = _f(4)
    if price is None or price <= 0:
        return None

    change_pct = _f(32)
    if change_pct is None and prev_close and prev_close > 0:
        change_pct = (price - prev_close) / prev_close * 100.0

    volume_lots = _f(6)
    turnover_wan = _f(37)
    market_cap_yi = _f(45)
    circ_cap_yi = _f(44)

    return {
        "price": price,
        "change_pct": change_pct,
        # Tencent reports volume in 手 (lots of 100); convert to 股 for parity
        # with the sina path so downstream signals see a consistent unit.
        "volume": volume_lots * 100 if volume_lots else None,
        "turnover": turnover_wan * 10_000 if turnover_wan else None,
        "turnover_rate": _f(38),
        "pe_ratio": _f(39),
        "pb_ratio": _f(46),
        "market_cap": market_cap_yi * 1e8 if market_cap_yi else None,
        "circ_market_cap": circ_cap_yi * 1e8 if circ_cap_yi else None,
    }
