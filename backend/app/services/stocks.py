"""A-share stock metadata lookup via akshare.

Strategy: validate format inline (instant), then resolve name per-stock via
`stock_individual_info_em` with a thread pool. We tried the bulk endpoint
`stock_zh_a_spot_em` but its host (82.push2.eastmoney.com) gets blocked by
some HTTPS proxies; per-stock calls hit a different host that is reliable.

Resolved (code, name) pairs are cached forever in process — stock names
change rarely, and the cache is rebuilt on restart.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable, Literal

import akshare as ak

logger = logging.getLogger(__name__)

CODE_RE = re.compile(r"^\d{6}$")
_name_cache: dict[str, str] = {}

# Result of a per-code lookup attempt. Carries success or the *kind* of
# failure so the import UI can tell the user "retryable" vs "actually wrong".
LookupOutcome = dict | Literal["invalid_format", "lookup_failed"]

LOOKUP_RETRIES = 2  # total attempts; akshare is flaky enough that 1 retry helps a lot


def detect_exchange(code: str) -> str:
    """Map a 6-digit A-share code to its exchange.

    上交所: 60xxxx, 688xxx (科创板), 689xxx
    深交所: 00xxxx, 002xxx, 003xxx, 30xxxx (创业板)
    北交所: 8xxxxx, 4xxxxx
    """
    if code.startswith(("60", "68")):
        return "sh"
    if code.startswith(("00", "30")):
        return "sz"
    if code.startswith(("8", "4")):
        return "bj"
    return "unknown"


def _fetch_name(code: str) -> str | None:
    """Resolve a code's 股票简称. Retries on transient akshare failures."""
    if code in _name_cache:
        return _name_cache[code]
    last_err: Exception | None = None
    for attempt in range(LOOKUP_RETRIES):
        try:
            df = ak.stock_individual_info_em(symbol=code)
            match = df[df["item"] == "股票简称"]
            if len(match) == 0:
                # akshare succeeded but has no row — treat as "really not found",
                # don't retry. Returning None here is a definitive "no such code".
                return None
            name = str(match.iloc[0]["value"]).strip()
            if not name:
                return None
            _name_cache[code] = name
            return name
        except Exception as e:
            last_err = e
            if attempt < LOOKUP_RETRIES - 1:
                time.sleep(0.5 * (attempt + 1))
    logger.warning("stock name lookup failed for %s after %d attempts: %s",
                   code, LOOKUP_RETRIES, last_err)
    return None


def lookup_codes(codes: Iterable[str]) -> dict[str, LookupOutcome]:
    """Validate and resolve a batch of codes in parallel.

    Returns a dict mapping each input code to one of:
      - {"code", "name", "exchange"}: success
      - "invalid_format": doesn't match ^\\d{6}$
      - "lookup_failed": format ok, but akshare didn't return a name (transient
        network failure or — rarely — a delisted/non-existent code).
        Caller should let the user retry these.
    """
    code_list = [c.strip() for c in codes]
    out: dict[str, LookupOutcome] = {}

    # Format check is free; only spend network on syntactically valid codes.
    to_resolve: list[str] = []
    for c in code_list:
        if not CODE_RE.match(c):
            out[c] = "invalid_format"
        else:
            to_resolve.append(c)

    if to_resolve:
        with ThreadPoolExecutor(max_workers=min(20, len(to_resolve))) as pool:
            futures = {pool.submit(_fetch_name, c): c for c in to_resolve}
            for fut in as_completed(futures):
                c = futures[fut]
                name = fut.result()
                if name is None:
                    out[c] = "lookup_failed"
                else:
                    out[c] = {"code": c, "name": name, "exchange": detect_exchange(c)}

    # Preserve input order in the returned dict.
    return {c: out[c] for c in code_list}


def normalize_codes(raw: str) -> list[str]:
    """Split a free-form blob into deduped, ordered 6-digit codes.

    Accepts newlines, commas, spaces, tabs, semicolons as separators.
    Strips non-digits per token (so 'sh600519' -> '600519').
    """
    tokens = re.split(r"[\s,;]+", raw or "")
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        digits = re.sub(r"\D", "", t)
        if len(digits) == 6 and digits not in seen:
            seen.add(digits)
            result.append(digits)
    return result
