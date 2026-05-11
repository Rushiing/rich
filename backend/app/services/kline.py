"""Phase 9: K-line history + technical indicators.

Per stock we keep 60 daily candles (covers MA60 / RSI / etc.) refreshed
once a day at 16:30 BJT. Hand-rolled indicator formulas — pandas-ta isn't
on PyPI for Python 3.11 + arm64 right now, and these are <30 lines each.

Data source: Tencent qt.gtimg.cn's `web.ifzq.gtimg.cn/appstock/app/fqkline/get`
endpoint. We tried akshare's stock_zh_a_hist first; it hits push2his.
eastmoney.com which is blocked from Railway egress (95%+ failure rate),
so we switched to the Tencent variant (same vendor that powers our
realtime quote pulls and also reachable on Railway).

Tencent JSON shape: {data: {sh600519: {qfqday: [[date, open, close, high,
low, volume], ...]}}}. We pull `qfq` (前复权) for indicator continuity
across stock-split events.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone
from typing import Iterable
from urllib.error import URLError

import pandas as pd
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..db import SessionLocal, engine
from ..models import Kline, Watchlist

logger = logging.getLogger(__name__)

KLINE_WINDOW_DAYS = 90  # pull a bit more than 60 so MA60's first row is valid
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _tencent_symbol(code: str) -> str:
    if code.startswith(("60", "68")):
        return f"sh{code}"
    if code.startswith(("00", "30")):
        return f"sz{code}"
    # 北交所: 8xxxxx / 4xxxxx 老段, 920xxx 新段 (2024 起)
    if code.startswith(("8", "4", "92")):
        return f"bj{code}"
    return code


def _fetch_tencent_kline(code: str, count: int = KLINE_WINDOW_DAYS) -> pd.DataFrame | None:
    """Pull qfq daily K-line from Tencent. Returns DataFrame with columns
    date / open / close / high / low / volume (no change_pct or
    turnover_rate — Tencent's daily-K endpoint doesn't expose them; we
    leave those columns null in the DB row)."""
    sym = _tencent_symbol(code)
    url = f"{TENCENT_KLINE_URL}?param={sym},day,,,{count},qfq"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        logger.warning("kline tencent fetch failed for %s: %s", code, e)
        return None

    inner = payload.get("data", {}).get(sym, {})
    rows = inner.get("qfqday") or inner.get("day")
    if not rows:
        return None
    parsed = []
    for r in rows:
        if len(r) < 6:
            continue
        try:
            parsed.append({
                "date": str(r[0]),
                "open": float(r[1]),
                "close": float(r[2]),
                "high": float(r[3]),
                "low": float(r[4]),
                "volume": float(r[5]),
            })
        except (ValueError, TypeError):
            continue
    if not parsed:
        return None
    return pd.DataFrame(parsed)


# --- indicator formulas -----------------------------------------------------


def _sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length, min_periods=length).mean()


def _ema(series: pd.Series, length: int) -> pd.Series:
    """Standard pandas-style EMA (alpha = 2/(N+1))."""
    return series.ewm(span=length, adjust=False).mean()


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    dif = _ema(close, fast) - _ema(close, slow)
    dea = _ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def _boll(close: pd.Series, length: int = 20, mult: float = 2.0):
    mid = _sma(close, length)
    std = close.rolling(window=length, min_periods=length).std()
    up = mid + mult * std
    low = mid - mult * std
    return mid, up, low


def _kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9,
         k_smooth: int = 3, d_smooth: int = 3):
    """KDJ via the standard 9/3/3 formula. K and D are 1/3-smoothed RSV."""
    high_n = high.rolling(window=n, min_periods=n).max()
    low_n = low.rolling(window=n, min_periods=n).min()
    rsv = ((close - low_n) / (high_n - low_n)) * 100
    rsv = rsv.fillna(50)
    # Wilder-style 1/3 smoothing == EMA with alpha=1/3
    k = rsv.ewm(alpha=1.0 / k_smooth, adjust=False).mean()
    d = k.ewm(alpha=1.0 / d_smooth, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def _rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    # Wilder's smoothing
    avg_up = up.ewm(alpha=1.0 / length, adjust=False).mean()
    avg_down = down.ewm(alpha=1.0 / length, adjust=False).mean()
    rs = avg_up / avg_down.replace(0, 1e-12)
    return 100 - (100 / (1 + rs))


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all indicator columns to a daily-K-line DataFrame in-place.
    Expects columns: open, close, high, low, volume. Returns the same df."""
    close = df["close"]
    df["ma5"] = _sma(close, 5)
    df["ma10"] = _sma(close, 10)
    df["ma20"] = _sma(close, 20)
    df["ma60"] = _sma(close, 60)
    dif, dea, hist = _macd(close)
    df["macd_dif"] = dif
    df["macd_dea"] = dea
    df["macd_hist"] = hist
    mid, up, low = _boll(close)
    df["boll_mid"] = mid
    df["boll_up"] = up
    df["boll_low"] = low
    k, d, j = _kdj(df["high"], df["low"], close)
    df["kdj_k"] = k
    df["kdj_d"] = d
    df["kdj_j"] = j
    df["rsi6"] = _rsi(close, 6)
    df["rsi12"] = _rsi(close, 12)
    return df


# --- akshare pull + DB upsert -----------------------------------------------


def pull_one(code: str) -> int:
    """Pull recent K-line for `code`, compute indicators, upsert into DB.
    Returns count of rows touched. Best-effort — failures are logged, not
    raised, so a flaky single code can't sink the daily batch."""
    df = _fetch_tencent_kline(code, count=KLINE_WINDOW_DAYS)
    if df is None or len(df) == 0:
        logger.warning("kline: no data for %s", code)
        return 0

    df = df.tail(KLINE_WINDOW_DAYS).reset_index(drop=True)
    df = compute_indicators(df)
    df["date"] = df["date"].astype(str).str[:10]

    db: Session = SessionLocal()
    try:
        rows = []
        now = datetime.now(timezone.utc)
        for r in df.itertuples(index=False):
            rows.append({
                "code": code,
                "date": r.date,
                "open": _f(r.open), "close": _f(r.close),
                "high": _f(r.high), "low": _f(r.low),
                "volume": _f(r.volume),
                "change_pct": _f(getattr(r, "change_pct", None)),
                "turnover_rate": _f(getattr(r, "turnover_rate", None)),
                "ma5": _f(r.ma5), "ma10": _f(r.ma10),
                "ma20": _f(r.ma20), "ma60": _f(r.ma60),
                "macd_dif": _f(r.macd_dif), "macd_dea": _f(r.macd_dea),
                "macd_hist": _f(r.macd_hist),
                "boll_mid": _f(r.boll_mid), "boll_up": _f(r.boll_up),
                "boll_low": _f(r.boll_low),
                "kdj_k": _f(r.kdj_k), "kdj_d": _f(r.kdj_d), "kdj_j": _f(r.kdj_j),
                "rsi6": _f(r.rsi6), "rsi12": _f(r.rsi12),
                "updated_at": now,
            })
        if not rows:
            return 0
        # Postgres / SQLite both support INSERT ON CONFLICT DO UPDATE; pick
        # the dialect-specific helper at runtime.
        if engine.dialect.name == "postgresql":
            stmt = pg_insert(Kline).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code", "date"],
                set_={c: stmt.excluded[c] for c in rows[0] if c not in ("code", "date")},
            )
        else:
            stmt = sqlite_insert(Kline).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["code", "date"],
                set_={c: stmt.excluded[c] for c in rows[0] if c not in ("code", "date")},
            )
        db.execute(stmt)
        db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        logger.exception("kline: upsert failed for %s", code)
        return 0
    finally:
        db.close()


def pull_for_watchlist() -> dict:
    """Refresh K-lines for every watched code. Sequential — 60 codes × ~1s
    each ≈ 1min. Run once per day at 16:30 BJT (post-close)."""
    db: Session = SessionLocal()
    try:
        codes = [c[0] for c in db.query(Watchlist.code).distinct().all()]
    finally:
        db.close()
    if not codes:
        return {"codes": 0, "updated": 0, "failed": 0}

    updated = 0
    failed = 0
    for code in codes:
        n = pull_one(code)
        if n > 0:
            updated += 1
        else:
            failed += 1
    logger.info("kline pull: codes=%d updated=%d failed=%d",
                len(codes), updated, failed)
    return {"codes": len(codes), "updated": updated, "failed": failed}


def latest_for_code(code: str) -> Kline | None:
    """Most recent K-line row for a code; used by the analysis prompt."""
    db: Session = SessionLocal()
    try:
        return (
            db.query(Kline)
            .filter(Kline.code == code)
            .order_by(Kline.date.desc())
            .first()
        )
    finally:
        db.close()


def recent_for_code(code: str, days: int = 20) -> list[Kline]:
    """Last N days of K-line rows for a code, ascending (oldest first).

    Used by the analysis prompt to feed the LLM a short price history so
    it can reason about trend/box/divergence rather than just the latest
    snapshot. Returns fewer rows when history is short; caller handles
    empty list.
    """
    db: Session = SessionLocal()
    try:
        # Pull descending, slice, then reverse — straightforward and the row
        # count is tiny (≤30) so order-by perf doesn't matter.
        rows = (
            db.query(Kline)
            .filter(Kline.code == code)
            .order_by(Kline.date.desc())
            .limit(days)
            .all()
        )
        rows.reverse()
        return rows
    finally:
        db.close()


def latest_indicators_for_codes(codes: Iterable[str]) -> dict[str, dict]:
    """Return {code: {close, ma5/10/20/60, macd_dif/dea/hist, macd_dif_prev,
    macd_dea_prev, rsi6/12, kdj_k/d/j, high20}}. high20 is max(close) over
    the last 20 daily K-lines so the 突破20日新高 signal can fire without
    storing it as a column. Codes with <2 K-line rows return {} (insufficient
    history for cross detection)."""
    codes = list(codes)
    if not codes:
        return {}
    db: Session = SessionLocal()
    try:
        # Pull last 20 rows per code in a single query, sort ascending,
        # bucket by code in Python.
        from sqlalchemy import func as sa_func
        # Subquery of (code, date) ordered desc, take per-code top 20.
        # Doing it portably (no LATERAL): pull all rows for these codes from
        # the last ~30 calendar days, then trim per-code in Python. With ~60
        # codes × 30 days = ~1800 rows that's cheap.
        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.now().date() - _td(days=45)).isoformat()
        rows = (
            db.query(Kline)
            .filter(Kline.code.in_(codes), Kline.date >= cutoff)
            .order_by(Kline.code, Kline.date.asc())
            .all()
        )
    finally:
        db.close()

    by_code: dict[str, list[Kline]] = {}
    for r in rows:
        by_code.setdefault(r.code, []).append(r)

    out: dict[str, dict] = {}
    for code, rows_for in by_code.items():
        if len(rows_for) < 2:
            continue
        latest = rows_for[-1]
        prev = rows_for[-2]
        last20 = rows_for[-20:]
        high20 = max((r.close for r in last20 if r.close is not None), default=None)
        out[code] = {
            "date": latest.date,
            "close": latest.close,
            "ma5": latest.ma5, "ma10": latest.ma10,
            "ma20": latest.ma20, "ma60": latest.ma60,
            "macd_dif": latest.macd_dif, "macd_dea": latest.macd_dea,
            "macd_hist": latest.macd_hist,
            "macd_dif_prev": prev.macd_dif, "macd_dea_prev": prev.macd_dea,
            "rsi6": latest.rsi6, "rsi12": latest.rsi12,
            "kdj_k": latest.kdj_k, "kdj_d": latest.kdj_d, "kdj_j": latest.kdj_j,
            "boll_mid": latest.boll_mid, "boll_up": latest.boll_up, "boll_low": latest.boll_low,
            "high20": high20,
        }
    return out


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f
