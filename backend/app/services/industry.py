"""Industry metadata + per-code percentile computation.

Two responsibilities:

1. `refresh_industry_meta(codes)` — populates the `industry_meta` table by
   pulling akshare's stock_individual_info_em (which returns 行业 in its
   output) per code. Slow path (~1s/code) so we only call it for codes
   that have no row yet OR are >7 days stale. Run weekly + at startup.

2. `compute_industry_context(snapshots)` — given a list of latest-per-code
   snapshot dicts, returns enriched dicts with the four percentile +
   average fields filled. Pure-Python ranking; no extra network calls.
   Caller writes the result back into Snapshot rows in cron.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import akshare as ak
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import IndustryMeta
from .scraper import _safe_with_timeout

logger = logging.getLogger(__name__)

REFRESH_AGE_DAYS = 7  # consider rows older than this stale + worth re-pulling


def _fetch_industry(code: str) -> str | None:
    df = _safe_with_timeout(ak.stock_individual_info_em, symbol=code, _timeout=8.0)
    if df is None or len(df) == 0:
        return None
    try:
        match = df[df["item"] == "行业"]
        if len(match) == 0:
            return None
        v = str(match.iloc[0]["value"]).strip()
        return v or None
    except Exception:
        return None


def refresh_industry_meta(codes: Iterable[str] | None = None) -> dict:
    """Upsert industry_meta rows for `codes` (default: all rows in watchlist).
    Skips codes that were updated within REFRESH_AGE_DAYS. Returns counters.
    """
    db: Session = SessionLocal()
    try:
        if codes is None:
            from ..models import Watchlist
            codes = [w[0] for w in db.query(Watchlist.code).distinct().all()]
        codes = list(codes)
        if not codes:
            return {"refreshed": 0, "skipped": 0, "failed": 0}

        cutoff = datetime.now(timezone.utc) - timedelta(days=REFRESH_AGE_DAYS)
        existing = {
            r.code: r for r in
            db.query(IndustryMeta).filter(IndustryMeta.code.in_(codes)).all()
        }
        refreshed = skipped = failed = 0
        for code in codes:
            row = existing.get(code)
            if row is not None:
                ua = row.updated_at
                if ua and ua.tzinfo is None:
                    ua = ua.replace(tzinfo=timezone.utc)
                if ua and ua >= cutoff:
                    skipped += 1
                    continue
            industry = _fetch_industry(code)
            if industry is None:
                failed += 1
                continue
            if row is None:
                db.add(IndustryMeta(code=code, industry_name=industry))
            else:
                row.industry_name = industry
                row.updated_at = datetime.now(timezone.utc)
            refreshed += 1
        db.commit()
        logger.info("industry_meta: refreshed=%d skipped=%d failed=%d",
                    refreshed, skipped, failed)
        return {"refreshed": refreshed, "skipped": skipped, "failed": failed}
    finally:
        db.close()


def get_industry_map(codes: Iterable[str] | None = None) -> dict[str, str]:
    """Return {code: industry_name} for codes that have a row."""
    db: Session = SessionLocal()
    try:
        q = db.query(IndustryMeta.code, IndustryMeta.industry_name)
        if codes is not None:
            codes = list(codes)
            if not codes:
                return {}
            q = q.filter(IndustryMeta.code.in_(codes))
        return {c: n for c, n in q.all()}
    finally:
        db.close()


def _percentile_rank(value: float, sorted_pool: list[float]) -> float:
    """0-100 percentile of `value` within `sorted_pool` (ascending). Larger
    value → higher percentile. Ties get the same rank.

    Empty pool → 50 (neutral / no information). Single-element pool → 50."""
    n = len(sorted_pool)
    if n <= 1:
        return 50.0
    # Number of elements strictly less than value
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_pool[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    less = lo
    return less / (n - 1) * 100.0


def compute_industry_context(
    snapshots: list[dict[str, Any]],
    industry_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Enrich each snapshot dict with industry_name + per-industry
    percentiles + averages. `snapshots` should each have at least
    {code, pe_ratio, pb_ratio, change_pct_3d, net_flow_3d}. Returns the
    same list with new keys added in place. Codes without industry
    mapping get industry_name=None and all percentile fields None.

    Pool definition: percentiles + averages are computed *within the
    snapshot list provided* — typically the latest-per-code snapshot for
    every watched stock. With ~50-100 codes spread across a dozen
    industries, an industry might have only 2-3 codes; we still emit
    percentiles based on that small pool because pinning percentiles
    against the FULL market would require pulling 5000+ snapshots per
    cron tick.
    """
    if industry_map is None:
        industry_map = get_industry_map([s["code"] for s in snapshots])

    # Group by industry
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in snapshots:
        ind = industry_map.get(s["code"])
        if ind:
            groups[ind].append(s)

    # Pre-sort each industry's distributions
    sorted_pe: dict[str, list[float]] = {}
    sorted_change: dict[str, list[float]] = {}
    sorted_flow: dict[str, list[float]] = {}
    avg_pe: dict[str, float | None] = {}
    avg_pb: dict[str, float | None] = {}
    for ind, members in groups.items():
        pe_pool = sorted([float(s["pe_ratio"]) for s in members
                          if s.get("pe_ratio") is not None])
        chg_pool = sorted([float(s["change_pct_3d"]) for s in members
                           if s.get("change_pct_3d") is not None])
        flow_pool = sorted([float(s["net_flow_3d"]) for s in members
                            if s.get("net_flow_3d") is not None])
        pb_pool = [float(s["pb_ratio"]) for s in members
                   if s.get("pb_ratio") is not None]
        sorted_pe[ind] = pe_pool
        sorted_change[ind] = chg_pool
        sorted_flow[ind] = flow_pool
        avg_pe[ind] = sum(pe_pool) / len(pe_pool) if pe_pool else None
        avg_pb[ind] = sum(pb_pool) / len(pb_pool) if pb_pool else None

    for s in snapshots:
        ind = industry_map.get(s["code"])
        s["industry_name"] = ind
        if not ind:
            s["industry_pe_pctile"] = None
            s["industry_change_3d_pctile"] = None
            s["industry_flow_3d_pctile"] = None
            s["industry_pe_avg"] = None
            s["industry_pb_avg"] = None
            continue
        pe = s.get("pe_ratio")
        chg = s.get("change_pct_3d")
        flow = s.get("net_flow_3d")
        s["industry_pe_pctile"] = (
            _percentile_rank(float(pe), sorted_pe[ind]) if pe is not None else None
        )
        s["industry_change_3d_pctile"] = (
            _percentile_rank(float(chg), sorted_change[ind]) if chg is not None else None
        )
        s["industry_flow_3d_pctile"] = (
            _percentile_rank(float(flow), sorted_flow[ind]) if flow is not None else None
        )
        s["industry_pe_avg"] = avg_pe[ind]
        s["industry_pb_avg"] = avg_pb[ind]

    return snapshots
