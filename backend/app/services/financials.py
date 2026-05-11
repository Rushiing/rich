"""Financial-statements digest for the analysis prompt.

Pipeline:
  1. Pull akshare's `stock_financial_abstract` (sina) — a wide table:
     rows = indicators, columns = report periods YYYYMMDD
  2. Extract ~10 key fields per period (revenue, profit, margins, YoY)
  3. Upsert into `financials` (code, report_date PK) — keep last 8 quarters

Refresh cadence:
  - Manual: POST /api/_diag/refresh-financials
  - Cron: weekly Monday 08:00 BJT (heaviest during earnings windows;
    inside one of those windows we may want to switch to daily — TODO)

Performance note: sina is per-stock ~1-3s each. We use a small thread
pool (4 workers) — going higher trips sina's per-IP rate limit and
actually makes the batch slower. Progress is published to a global
counter so /status can show running totals.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Iterable

import akshare as ak
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Financial
from .scraper import _safe_with_timeout

# Live progress shared with the diag /status endpoint.
_progress_lock = threading.Lock()
_progress: dict[str, Any] = {"done": 0, "ok": 0, "failed": 0, "total": 0, "current": None}


def get_progress() -> dict[str, Any]:
    """Snapshot of in-flight batch progress. Empty dict outside a run."""
    with _progress_lock:
        return dict(_progress)

logger = logging.getLogger(__name__)

# How many recent quarters to keep per stock. 8 = 2 years of context.
KEEP_QUARTERS = 8


def _row_at(df, indicator: str, category: str | None = None):
    """Find a row by indicator name (optionally narrowed by category like
    '常用指标'). Returns the pandas Series (one row of the wide table) or
    None when missing. Some indicator names appear in multiple categories
    (e.g. 毛利率 is in both 常用指标 and 盈利能力); narrowing by category
    keeps us consistent."""
    mask = df["指标"] == indicator
    if category:
        mask = mask & (df["选项"] == category)
    matches = df[mask]
    if len(matches) == 0:
        return None
    return matches.iloc[0]


def _f(v) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        # Sina sometimes ships "--" or huge sentinel values; skip nan
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def pull_for_code(code: str) -> int:
    """Fetch financials for one stock, upsert latest KEEP_QUARTERS rows.
    Returns count of rows written. Returns 0 on any failure (logged)."""
    import time as _time
    _t0 = _time.monotonic()
    df = _safe_with_timeout(ak.stock_financial_abstract, symbol=code, _timeout=15.0)
    fetch_ms = int((_time.monotonic() - _t0) * 1000)
    if df is None or len(df) == 0:
        logger.info("financials[%s] fetch=%dms result=empty", code, fetch_ms)
        return 0

    # Pre-extract each indicator row once
    revenue_r       = _row_at(df, "营业总收入", "常用指标")
    net_profit_r    = _row_at(df, "归母净利润", "常用指标")
    excl_nr_r       = _row_at(df, "扣非净利润", "常用指标")
    gross_margin_r  = _row_at(df, "毛利率", "常用指标")
    net_margin_r    = _row_at(df, "销售净利率", "常用指标")
    roe_r           = _row_at(df, "净资产收益率(ROE)", "常用指标")
    revenue_yoy_r   = _row_at(df, "营业总收入增长率", "成长能力")
    profit_yoy_r    = _row_at(df, "归属母公司净利润增长率", "成长能力")
    debt_ratio_r    = _row_at(df, "资产负债率", "常用指标")
    expense_ratio_r = _row_at(df, "期间费用率", "常用指标")

    # The period columns are all 8-digit date strings like '20260331'.
    # Filter columns to those + sort desc, then keep top KEEP_QUARTERS.
    period_cols = [c for c in df.columns
                   if isinstance(c, str) and len(c) == 8 and c.isdigit()]
    period_cols.sort(reverse=True)
    period_cols = period_cols[:KEEP_QUARTERS]

    if not period_cols:
        return 0

    db: Session = SessionLocal()
    written = 0
    try:
        now = datetime.now(timezone.utc)
        for period in period_cols:
            existing = (
                db.query(Financial)
                .filter(Financial.code == code, Financial.report_date == period)
                .first()
            )
            kwargs = dict(
                total_revenue=_f(revenue_r.get(period) if revenue_r is not None else None),
                net_profit=_f(net_profit_r.get(period) if net_profit_r is not None else None),
                net_profit_excl_nr=_f(excl_nr_r.get(period) if excl_nr_r is not None else None),
                gross_margin=_f(gross_margin_r.get(period) if gross_margin_r is not None else None),
                net_margin=_f(net_margin_r.get(period) if net_margin_r is not None else None),
                roe=_f(roe_r.get(period) if roe_r is not None else None),
                revenue_yoy=_f(revenue_yoy_r.get(period) if revenue_yoy_r is not None else None),
                profit_yoy=_f(profit_yoy_r.get(period) if profit_yoy_r is not None else None),
                debt_ratio=_f(debt_ratio_r.get(period) if debt_ratio_r is not None else None),
                expense_ratio=_f(expense_ratio_r.get(period) if expense_ratio_r is not None else None),
                updated_at=now,
            )
            # If all metrics are None, skip — empty row provides no value
            if all(v is None for k, v in kwargs.items() if k != "updated_at"):
                continue
            if existing is None:
                db.add(Financial(code=code, report_date=period, **kwargs))
            else:
                for k, v in kwargs.items():
                    setattr(existing, k, v)
            written += 1
        db.commit()
    except Exception as e:
        logger.exception("financials write failed for %s: %s", code, e)
        db.rollback()
        return 0
    finally:
        db.close()
    total_ms = int((_time.monotonic() - _t0) * 1000)
    logger.info("financials[%s] fetch=%dms total=%dms rows=%d",
                code, fetch_ms, total_ms, written)
    return written


# Sina rate-limits per-IP fairly aggressively; 4 concurrent workers is
# the sweet spot in practice — higher leads to 429-style stalls that
# make the whole batch slower.
DEFAULT_MAX_WORKERS = 4


def pull_for_watchlist(codes: Iterable[str] | None = None) -> dict:
    """Batch refresh — uses a small thread pool because sina rate-limits
    per IP. Default = all distinct codes in watchlist. Updates the global
    _progress dict so /api/_diag/refresh-financials/status can stream
    counts mid-run. Returns final counters."""
    if codes is None:
        db: Session = SessionLocal()
        try:
            from ..models import Watchlist
            codes = [w[0] for w in db.query(Watchlist.code).distinct().all()]
        finally:
            db.close()
    codes = list(codes)
    if not codes:
        return {"requested": 0, "ok": 0, "failed": 0, "rows": 0}

    with _progress_lock:
        _progress["total"] = len(codes)
        _progress["done"] = 0
        _progress["ok"] = 0
        _progress["failed"] = 0
        _progress["current"] = None

    ok = failed = rows = 0
    with ThreadPoolExecutor(max_workers=min(DEFAULT_MAX_WORKERS, len(codes))) as pool:
        futures = {pool.submit(pull_for_code, c): c for c in codes}
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                n = fut.result()
                if n > 0:
                    ok += 1
                    rows += n
                else:
                    failed += 1
            except Exception:
                logger.exception("financials thread for %s raised", code)
                failed += 1
            with _progress_lock:
                _progress["done"] += 1
                _progress["ok"] = ok
                _progress["failed"] = failed
                _progress["current"] = code

    logger.info("financials batch: requested=%d ok=%d failed=%d rows=%d",
                len(codes), ok, failed, rows)
    return {"requested": len(codes), "ok": ok, "failed": failed, "rows": rows}


def latest_for_code(code: str, n: int = 2) -> list[Financial]:
    """Return latest N financial rows for a code, newest first.
    Used by the analysis prompt for the 财务面 section."""
    db: Session = SessionLocal()
    try:
        return (
            db.query(Financial)
            .filter(Financial.code == code)
            .order_by(Financial.report_date.desc())
            .limit(n)
            .all()
        )
    finally:
        db.close()
