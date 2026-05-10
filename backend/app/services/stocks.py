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
    北交所: 8xxxxx, 4xxxxx, 920xxx (新前缀, 2024 起)
    """
    if code.startswith(("60", "68")):
        return "sh"
    if code.startswith(("00", "30")):
        return "sz"
    if code.startswith(("8", "4", "92")):
        return "bj"
    return "unknown"


def _fetch_name_eastmoney(code: str) -> str | None:
    """Per-code lookup via akshare's `stock_individual_info_em`. Used only
    as a fallback when the primary Tencent batch path doesn't have the
    code — eastmoney's push2 host is blocked on Railway, so this almost
    always fails in production. Kept for dev/local where it works and
    for the rare delisted code that Tencent might also miss."""
    if code in _name_cache:
        return _name_cache[code]
    last_err: Exception | None = None
    for attempt in range(LOOKUP_RETRIES):
        try:
            df = ak.stock_individual_info_em(symbol=code)
            match = df[df["item"] == "股票简称"]
            if len(match) == 0:
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
    logger.warning("eastmoney lookup fallback failed for %s after %d attempts: %s",
                   code, LOOKUP_RETRIES, last_err)
    return None


def lookup_codes(codes: Iterable[str]) -> dict[str, LookupOutcome]:
    """Validate and resolve a batch of codes.

    Resolution order:
    1. In-process name cache.
    2. Tencent qt.gtimg.cn batch — one HTTP call covers up to 50 codes
       at once. Reachable from Railway (eastmoney's per-code info
       endpoint isn't), so this is the primary path.
    3. eastmoney per-code fallback for codes Tencent didn't recognize.
       Almost always fails on Railway; kept for dev parity.

    Returns a dict mapping each input code to one of:
      - {"code", "name", "exchange"}: success
      - "invalid_format": doesn't match ^\\d{6}$
      - "lookup_failed": format ok but no source had a name. Frontend
        offers a "retry" CTA on these so transient network blips don't
        force the user to re-paste.
    """
    code_list = [c.strip() for c in codes]
    out: dict[str, LookupOutcome] = {}

    to_resolve: list[str] = []
    for c in code_list:
        if not CODE_RE.match(c):
            out[c] = "invalid_format"
        else:
            to_resolve.append(c)

    # Pre-fill from name cache so a re-import after a partial failure is free.
    cached_now: dict[str, str] = {}
    still_unknown: list[str] = []
    for c in to_resolve:
        if c in _name_cache:
            cached_now[c] = _name_cache[c]
        else:
            still_unknown.append(c)

    # --- primary: Tencent batch (1 HTTP call per 50 codes) ---
    if still_unknown:
        # Late import to dodge a circular reference (realtime_quotes is in
        # the same package and imports from .scraper, which imports us).
        from .realtime_quotes import fetch_names_tencent
        tencent = fetch_names_tencent(still_unknown)
        for c, name in tencent.items():
            cached_now[c] = name
            _name_cache[c] = name
        still_unknown = [c for c in still_unknown if c not in tencent]

    # --- fallback: eastmoney per-code (parallel) ---
    if still_unknown:
        with ThreadPoolExecutor(max_workers=min(20, len(still_unknown))) as pool:
            futures = {pool.submit(_fetch_name_eastmoney, c): c for c in still_unknown}
            for fut in as_completed(futures):
                c = futures[fut]
                name = fut.result()
                if name is not None:
                    cached_now[c] = name

    # Build outcomes
    for c in to_resolve:
        name = cached_now.get(c)
        if name is None:
            out[c] = "lookup_failed"
        else:
            out[c] = {"code": c, "name": name, "exchange": detect_exchange(c)}

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
