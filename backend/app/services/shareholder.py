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

import requests
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import ShareholderChange, Watchlist

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

# 东方财富 datacenter URL — 跟 akshare.stock_hold_management_detail_em
# 用的同一个 endpoint,但我们自己 control pagination (akshare 内部一次拉
# 全表几十页,180s 都不够)。
_EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# Max pages to fetch per refresh. 5000 行/页, 2 页 = 10000 events,通常
# cover 最近 1-3 个月市场全量 (东财日均 100-300 个公告)。够 90 天 window。
MAX_PAGES = 2

# Per-page request timeout (seconds).
PAGE_TIMEOUT = 25


def _fetch_recent_insider_changes_from_em() -> list[dict[str, Any]]:
    """Direct call to 东方财富 datacenter, paginated, sorted by date DESC.
    Bypasses akshare's stock_hold_management_detail_em which paginates ALL
    pages internally (too slow). We cap at MAX_PAGES so wall time is
    bounded — 2 pages × ~10s = 20-30s typical."""
    base_params = {
        "reportName": "RPT_EXECUTIVE_HOLD_DETAILS",
        "columns": "ALL",
        "quoteColumns": "",
        "filter": "",
        "pageSize": "5000",
        # Sort: CHANGE_DATE DESC + SECURITY_CODE + PERSON_NAME (akshare 同款)
        "sortTypes": "-1,1,1",
        "sortColumns": "CHANGE_DATE,SECURITY_CODE,PERSON_NAME",
        "source": "WEB",
        "client": "WEB",
    }
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        params = {
            **base_params,
            "pageNumber": str(page),
            "p": str(page),
            "pageNo": str(page),
            "pageNum": str(page),
        }
        try:
            r = requests.get(_EM_URL, params=params, timeout=PAGE_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            result = data.get("result") or {}
            page_rows = result.get("data") or []
            if not page_rows:
                break
            rows.extend(page_rows)
            logger.info(
                "shareholder fetch: page %d → %d rows (total %d)",
                page, len(page_rows), len(rows),
            )
        except Exception as e:
            logger.warning("shareholder fetch: page %d failed: %s", page, e)
            break
    return rows


# Map 东财 raw column names → our schema field names (matches akshare's
# stock_hold_management_detail_em rename logic).
_FIELD_MAP = {
    "SECURITY_CODE": "code",
    "CHANGE_DATE": "change_date",
    "PERSON_NAME": "person",
    "CHANGE_SHARES": "change_shares",
    "AVERAGE_PRICE": "avg_price",
    "CHANGE_AMOUNT": "change_amount",
    "CHANGE_REASON": "change_reason",
    "CHANGE_RATIO": "change_pct",
    "CHANGE_AFTER_HOLDNUM": "holdings_after",
    "DSE_PERSON_NAME": "insider_name",
    "POSITION_NAME": "role",
    "PERSON_DSE_RELATION": "relation",
}


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
            "shareholder pull: fetching from 东财 datacenter (max %d pages × "
            "5000), will filter to %d watchlist codes",
            MAX_PAGES, len(codes),
        )

        raw_rows = _fetch_recent_insider_changes_from_em()
        rows_seen = len(raw_rows)
        with _progress_lock:
            _progress["rows_seen"] = rows_seen

        if rows_seen == 0:
            logger.warning("shareholder pull: 0 rows from 东财 endpoint")
            with _progress_lock:
                _progress["failed"] = 1
            return {
                "watchlist_codes": len(codes), "rows_seen": 0,
                "rows_upserted": 0, "failed": 1, "fetch_failed": True,
            }

        logger.info("shareholder pull: 东财 returned %d rows", rows_seen)

        cutoff_date = (
            datetime.now(timezone.utc).date() - timedelta(days=window_days)
        )

        upserts = 0
        for row in raw_rows:
            code = _s(row.get("SECURITY_CODE"), 6)
            if code is None or code not in codes:
                continue
            # CHANGE_DATE format from 东财: "2024-12-15 00:00:00"
            date_raw = _s(row.get("CHANGE_DATE"), 19)
            if date_raw is None:
                continue
            date_str = date_raw[:10]
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff_date:
                continue

            person = _s(row.get("PERSON_NAME"), 40) or "未知"
            shares = _f(row.get("CHANGE_SHARES"))

            existing = db.query(ShareholderChange).filter_by(
                code=code,
                change_date=date_str,
                person=person,
                change_shares=shares,
            ).first()

            attrs = {
                "code": code,
                "change_date": date_str,
                "person": person,
                "change_shares": shares,
                "avg_price": _f(row.get("AVERAGE_PRICE")),
                "change_amount": _f(row.get("CHANGE_AMOUNT")),
                "change_reason": _s(row.get("CHANGE_REASON"), 40),
                "change_pct": _f(row.get("CHANGE_RATIO")),
                "holdings_after": _f(row.get("CHANGE_AFTER_HOLDNUM")),
                "insider_name": _s(row.get("DSE_PERSON_NAME"), 40),
                "role": _s(row.get("POSITION_NAME"), 40),
                "relation": _s(row.get("PERSON_DSE_RELATION"), 20),
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
