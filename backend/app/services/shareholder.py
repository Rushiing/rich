"""Insider shareholding changes (董监高 / 高管 / 配偶子女增减持) for the
analysis prompt.

Pipeline:
  1. Pull akshare's `stock_hold_management_person_em` (东方财富 - 数据中心
     - 特色数据 - 人员增减持明细) — 市场全量,按时间倒序的事件流
  2. Filter to watchlist codes + last 90 days
  3. Upsert into `shareholder_changes`,unique by (code, date, person, shares)

Why this data source (6/9 corrected):
  Phase 0 probe initially tried stock_hold_management_person_em which
  defaults to symbol='001308' name='吴远' (single insider lookup) — that's
  why we only saw 4 rows. Reading akshare source revealed
  stock_hold_management_detail_em is the correct market-wide endpoint:
  paginates 5000 rows/page server-side, returns full event-level history
  with 变动日期 + 变动股数 + 成交均价 + 变动金额 + 变动原因 + 职务 +
  与董监高关系 — exactly the LLM signal we want.

Refresh cadence:
  - Manual: POST /api/_diag/refresh-shareholder
  - Cron: daily mon-fri 17:30 BJT (after 17:00 outcomes tick)

Performance:
  - stock_hold_management_detail_em pagination: full table can run several
    pages × 5000 rows × 1-2s/page ≈ 1-3 min wall time
  - 180s timeout. Note: Railway 30s edge cap只对入站 HTTP,我们的
    refresh-shareholder endpoint 是 async background thread,出站
    akshare call 不受限制
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Any

import akshare as ak
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ShareholderChange, Watchlist
from .scraper import _safe_with_timeout

logger = logging.getLogger(__name__)

# Live progress shared with the diag /status endpoint.
_progress_lock = threading.Lock()
_progress: dict[str, Any] = {
    "done": 0, "rows_seen": 0, "rows_upserted": 0, "failed": 0,
}


def get_progress() -> dict[str, Any]:
    """Snapshot of in-flight progress. Resets at start of each batch."""
    with _progress_lock:
        return dict(_progress)


# Window of insider changes used by analysis.py prompt.
DEFAULT_WINDOW_DAYS = 90

# DB retention — keep more than analysis window for future hit-rate joins.
RETENTION_DAYS = 365


def _f(v) -> float | None:
    """Safe float conversion. Returns None on None/NaN/bad string."""
    try:
        if v is None:
            return None
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _s(v, max_len: int) -> str | None:
    """Safe string conversion + truncation. Empty/nan returns None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s[:max_len]


def pull_for_watchlist(window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
    """Pull market-wide insider change data, filter to watchlist codes
    + last window_days days, upsert into shareholder_changes.

    Returns counters: {watchlist_codes, rows_seen, rows_upserted, failed}.
    """
    db: Session = SessionLocal()
    try:
        codes = {c for (c,) in db.query(Watchlist.code).distinct().all()}
        if not codes:
            logger.info("shareholder pull: watchlist empty, skipping")
            return {
                "watchlist_codes": 0, "rows_seen": 0,
                "rows_upserted": 0, "failed": 0,
            }

        with _progress_lock:
            _progress.update({
                "done": 0, "rows_seen": 0, "rows_upserted": 0, "failed": 0,
            })

        logger.info(
            "shareholder pull: starting market-wide fetch "
            "(will filter to %d watchlist codes)",
            len(codes),
        )

        # Market-wide pull. 180s timeout: pagination 5000 rows/page can
        # run several pages. Async worker thread so Railway edge 30s
        # doesn't apply.
        df = _safe_with_timeout(
            ak.stock_hold_management_detail_em, _timeout=180.0,
        )
        if df is None:
            logger.warning("shareholder pull: akshare fetch timed out or errored")
            with _progress_lock:
                _progress["failed"] = 1
            return {
                "watchlist_codes": len(codes), "rows_seen": 0,
                "rows_upserted": 0, "failed": 1, "fetch_failed": True,
            }

        rows_seen = len(df)
        with _progress_lock:
            _progress["rows_seen"] = rows_seen
        logger.info("shareholder pull: akshare returned %d rows", rows_seen)

        cutoff_date = (
            datetime.now(timezone.utc).date() - timedelta(days=window_days)
        )

        upserts = 0
        for _, row in df.iterrows():
            code = _s(row.get("代码"), 6)
            if code is None or code not in codes:
                continue
            date_str = _s(row.get("日期"), 10)
            if date_str is None:
                continue
            try:
                d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff_date:
                continue

            person = _s(row.get("变动人"), 40) or "未知"
            shares = _f(row.get("变动股数"))

            existing = db.query(ShareholderChange).filter_by(
                code=code,
                change_date=date_str[:10],
                person=person,
                change_shares=shares,
            ).first()

            attrs = {
                "code": code,
                "change_date": date_str[:10],
                "person": person,
                "change_shares": shares,
                "avg_price": _f(row.get("成交均价")),
                "change_amount": _f(row.get("变动金额")),
                "change_reason": _s(row.get("变动原因"), 40),
                "change_pct": _f(row.get("变动比例")),
                "holdings_after": _f(row.get("变动后持股数")),
                "insider_name": _s(row.get("董监高人员姓名"), 40),
                "role": _s(row.get("职务"), 40),
                "relation": _s(row.get("变动人与董监高的关系"), 20),
            }
            if existing:
                for k, v in attrs.items():
                    if k != "id":
                        setattr(existing, k, v)
            else:
                db.add(ShareholderChange(**attrs))
            upserts += 1

            # Commit every 100 rows for crash safety + visible progress
            if upserts % 100 == 0:
                db.commit()
                with _progress_lock:
                    _progress["rows_upserted"] = upserts

        db.commit()

        # Prune ancient rows past retention
        prune_cutoff = (
            datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS)
        ).isoformat()
        pruned = db.query(ShareholderChange).filter(
            ShareholderChange.change_date < prune_cutoff
        ).delete(synchronize_session=False)
        db.commit()
        if pruned:
            logger.info("shareholder pull: pruned %d ancient rows", pruned)

        with _progress_lock:
            _progress["rows_upserted"] = upserts
            _progress["done"] = 1

        result = {
            "watchlist_codes": len(codes),
            "rows_seen": rows_seen,
            "rows_upserted": upserts,
            "failed": 0,
            "pruned": pruned,
        }
        logger.info("shareholder pull: done %s", result)
        return result
    except Exception as e:
        logger.exception("shareholder pull failed: %s", e)
        with _progress_lock:
            _progress["failed"] = 1
        return {"failed": 1, "error": str(e)}
    finally:
        db.close()


def latest_for_code(
    code: str,
    days: int = DEFAULT_WINDOW_DAYS,
    n: int = 20,
) -> list[ShareholderChange]:
    """Return latest N shareholder changes for code in last `days` days,
    newest first. Used by analysis.py prompt to build the 股东变动 section.
    """
    cutoff = (
        datetime.now(timezone.utc).date() - timedelta(days=days)
    ).isoformat()
    db: Session = SessionLocal()
    try:
        return (
            db.query(ShareholderChange)
            .filter(
                ShareholderChange.code == code,
                ShareholderChange.change_date >= cutoff,
            )
            .order_by(desc(ShareholderChange.change_date))
            .limit(n)
            .all()
        )
    finally:
        db.close()
