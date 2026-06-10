"""Analysis-outcome tracking — the feedback loop.

Each time an analysis is generated we drop an anchor row (verdict +
reference price). The daily cron then fills in forward closing prices at
+1 / +3 / +5 / +20 trading days, so over time we can answer "did the
建议买入 calls actually go up?" — and compare hit rate across prompt
versions / single-vs-debate modes.

Forward prices come from the `klines` table (daily qfq close), so "N
trading days later" is just "the Nth kline row dated after generated_at".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import AnalysisOutcome, Kline

logger = logging.getLogger(__name__)

# The day-offsets we track. Keep in sync with the close_dN / return_dN
# columns on AnalysisOutcome.
HORIZONS = [1, 3, 5, 20]


def record_anchor(
    db: Session, code: str, generated_at: datetime, actionable: str,
    prompt_version: str | None, mode: str | None, anchor_price: float | None,
    confidence: int | None = None,
    data_completeness: int | None = None,
    model: str | None = None,
    nd_trend: str | None = None,
    nd_confidence: str | None = None,
) -> None:
    """Insert an outcome anchor. Called from analysis.generate() right
    after the Analysis row is persisted. No-op when anchor_price is
    missing — without a reference price we can't measure return.

    5/29: also stores confidence + data_completeness per anchor so the
    detail-page "历史解析" card can show how those values evolved
    across regenerations. Both default to None for call sites that
    haven't been updated (e.g. unit tests, batch backfills).

    6/10: model (A/B bucketing) + nd_trend/nd_confidence (next_day_outlook
    scoring — see nd_outlook_stats). All optional, None for legacy sites."""
    if anchor_price is None or anchor_price <= 0:
        logger.info("outcome anchor skipped for %s — no anchor price", code)
        return
    # Normalize legacy enum confidence ("高"/"中"/"低") to int — anchor
    # rows should be uniformly numeric so the history table doesn't
    # have to render mixed types.
    if isinstance(confidence, str):
        confidence = {"高": 85, "中": 65, "低": 45}.get(confidence)
    db.add(AnalysisOutcome(
        code=code,
        generated_at=generated_at,
        actionable=actionable or "",
        prompt_version=prompt_version,
        mode=mode,
        anchor_price=anchor_price,
        confidence=confidence,
        data_completeness=data_completeness,
        model=model,
        nd_trend=nd_trend,
        nd_confidence=nd_confidence,
    ))
    db.commit()


def backfill_outcomes() -> dict:
    """Walk outcomes with unfilled horizons, fill close_dN / return_dN from
    the klines table. Idempotent — only fills columns that are still NULL
    and have enough trading days elapsed. Returns counters."""
    db: Session = SessionLocal()
    filled = scanned = 0
    try:
        # Rows with at least one unfilled horizon, or missing the
        # anchor_close basis (legacy rows predating 6/10).
        from sqlalchemy import or_
        rows = (
            db.query(AnalysisOutcome)
            .filter(or_(
                AnalysisOutcome.close_d20.is_(None),
                AnalysisOutcome.anchor_close.is_(None),
            ))
            .all()
        )
        for o in rows:
            scanned += 1
            gen_date = o.generated_at
            if gen_date.tzinfo is None:
                gen_date = gen_date.replace(tzinfo=timezone.utc)
            gen_day = gen_date.date().isoformat()

            changed = False

            # anchor_close: qfq close of the anchor's trading day (latest
            # kline ≤ gen_day — falls back to the prior trading day when
            # generated on a weekend/holiday). Same price series as
            # close_dN, so returns computed from it are dividend-safe,
            # unlike anchor_price (unadjusted intraday).
            if o.anchor_close is None:
                anchor_bar = (
                    db.query(Kline)
                    .filter(Kline.code == o.code, Kline.date <= gen_day)
                    .order_by(Kline.date.desc())
                    .first()
                )
                if anchor_bar is not None and anchor_bar.close is not None:
                    o.anchor_close = anchor_bar.close
                    changed = True

            # Trading days strictly after the generation date, ascending.
            future = (
                db.query(Kline)
                .filter(Kline.code == o.code, Kline.date > gen_day)
                .order_by(Kline.date.asc())
                .all()
            )
            for h in HORIZONS:
                close_attr = f"close_d{h}"
                return_attr = f"return_d{h}"
                if getattr(o, close_attr) is not None:
                    continue  # already filled
                if len(future) < h:
                    continue  # not enough trading days yet
                bar = future[h - 1]
                if bar.close is None:
                    continue
                setattr(o, close_attr, bar.close)
                setattr(o, return_attr,
                        (bar.close - o.anchor_price) / o.anchor_price * 100.0)
                changed = True
            if changed:
                o.updated_at = datetime.now(timezone.utc)
                filled += 1
        db.commit()
    finally:
        db.close()
    logger.info("outcomes backfill: scanned=%d filled=%d", scanned, filled)
    return {"scanned": scanned, "filled": filled}


def hit_rate_stats() -> dict[str, Any]:
    """Compute hit-rate summary grouped by actionable verdict + prompt
    version. A 'hit' for 建议买入 = return_d5 > 0; for 建议卖出 =
    return_d5 < 0; others are not scored (no directional claim).

    Returns a dict the diag endpoint serializes directly."""
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(AnalysisOutcome)
            .filter(AnalysisOutcome.return_d5.isnot(None))
            .all()
        )
    finally:
        db.close()

    # Bucket by (prompt_version, actionable)
    buckets: dict[tuple, dict[str, Any]] = {}
    for o in rows:
        key = (o.prompt_version or "?", o.actionable)
        b = buckets.setdefault(key, {
            "prompt_version": o.prompt_version or "?",
            "actionable": o.actionable,
            "n": 0, "hits": 0, "sum_return_d5": 0.0,
        })
        b["n"] += 1
        b["sum_return_d5"] += o.return_d5
        if o.actionable == "建议买入" and o.return_d5 > 0:
            b["hits"] += 1
        elif o.actionable == "建议卖出" and o.return_d5 < 0:
            b["hits"] += 1

    summary = []
    for b in buckets.values():
        n = b["n"]
        directional = b["actionable"] in ("建议买入", "建议卖出")
        summary.append({
            "prompt_version": b["prompt_version"],
            "actionable": b["actionable"],
            "n": n,
            "hit_rate": round(b["hits"] / n * 100, 1) if (directional and n) else None,
            "avg_return_d5": round(b["sum_return_d5"] / n, 2) if n else None,
        })
    summary.sort(key=lambda x: (x["prompt_version"], x["actionable"]))
    return {"total_scored": len(rows), "buckets": summary}


def hit_rate_by_confidence() -> dict[str, Any]:
    """Stratify hit_rate by confidence bucket across d1/d3/d5 horizons.

    Tests whether the LLM's self-reported confidence actually correlates
    with accuracy — i.e. whether 5/28's confidence-as-int system is
    doing real work or is just decoration.

    Buckets follow frontend's confidenceBucket():
      high: >= 80
      med:  60-79
      low:  < 60

    6/3 — returns d1/d3/d5 horizons in one shot. Confidence column on
    outcomes was added 5/29; the first d5-scored anchors with non-null
    confidence won't appear until ~6/5. d1/d3 light up earlier and give
    a preview of whether the field is meaningful. d5 stays the gold
    standard (hit_rate_stats uses d5 too).

    Expected pattern if confidence is meaningful:
      high.hit_rate > med.hit_rate > low.hit_rate
    Flat distribution = LLM throwing dice picking numbers.

    Only buy/sell anchors (directional). Anchors lacking confidence are
    excluded entirely (legacy).
    """
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(AnalysisOutcome)
            .filter(
                AnalysisOutcome.confidence.isnot(None),
                AnalysisOutcome.actionable.in_(["建议买入", "建议卖出"]),
            )
            .all()
        )
    finally:
        db.close()

    def bucket(c: int) -> str:
        if c >= 80:
            return "high"
        if c >= 60:
            return "med"
        return "low"

    def is_hit(actionable: str, ret: float) -> bool:
        if actionable == "建议买入":
            return ret > 0
        return ret < 0  # 建议卖出

    # Each bucket accumulates per-horizon counts.
    HORIZONS = ("d1", "d3", "d5")
    buckets: dict[tuple, dict[str, Any]] = {}
    for o in rows:
        key = (o.actionable, bucket(o.confidence))
        b = buckets.setdefault(key, {
            "actionable": o.actionable,
            "confidence_bucket": bucket(o.confidence),
            **{h: {"n": 0, "hits": 0, "sum": 0.0} for h in HORIZONS},
        })
        for h in HORIZONS:
            ret = getattr(o, f"return_{h}")
            if ret is None:
                continue
            b[h]["n"] += 1
            b[h]["sum"] += ret
            if is_hit(o.actionable, ret):
                b[h]["hits"] += 1

    summary = []
    for b in buckets.values():
        item = {
            "actionable": b["actionable"],
            "confidence_bucket": b["confidence_bucket"],
        }
        for h in HORIZONS:
            hb = b[h]
            n = hb["n"]
            item[h] = {
                "n": n,
                "hit_rate": round(hb["hits"] / n * 100, 1) if n else None,
                "avg_return": round(hb["sum"] / n, 2) if n else None,
            }
        summary.append(item)

    bucket_order = {"high": 0, "med": 1, "low": 2}
    summary.sort(key=lambda x: (x["actionable"], bucket_order[x["confidence_bucket"]]))

    # Roll-up totals per horizon: how many anchors of any bucket are
    # currently scored at that horizon. Useful for "do I have enough
    # sample to trust the comparison?"
    totals = {h: sum(b[h]["n"] for b in buckets.values()) for h in HORIZONS}

    return {
        "total_with_confidence": len(rows),  # row count irrespective of horizon
        "scored_per_horizon": totals,
        "buckets": summary,
    }
