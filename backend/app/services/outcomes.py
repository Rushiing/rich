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
) -> None:
    """Insert an outcome anchor. Called from analysis.generate() right
    after the Analysis row is persisted. No-op when anchor_price is
    missing — without a reference price we can't measure return."""
    if anchor_price is None or anchor_price <= 0:
        logger.info("outcome anchor skipped for %s — no anchor price", code)
        return
    db.add(AnalysisOutcome(
        code=code,
        generated_at=generated_at,
        actionable=actionable or "",
        prompt_version=prompt_version,
        mode=mode,
        anchor_price=anchor_price,
    ))
    db.commit()


def backfill_outcomes() -> dict:
    """Walk outcomes with unfilled horizons, fill close_dN / return_dN from
    the klines table. Idempotent — only fills columns that are still NULL
    and have enough trading days elapsed. Returns counters."""
    db: Session = SessionLocal()
    filled = scanned = 0
    try:
        # Only rows that still have at least one unfilled horizon.
        rows = (
            db.query(AnalysisOutcome)
            .filter(AnalysisOutcome.close_d20.is_(None))
            .all()
        )
        for o in rows:
            scanned += 1
            gen_date = o.generated_at
            if gen_date.tzinfo is None:
                gen_date = gen_date.replace(tzinfo=timezone.utc)
            gen_day = gen_date.date().isoformat()

            # Trading days strictly after the generation date, ascending.
            future = (
                db.query(Kline)
                .filter(Kline.code == o.code, Kline.date > gen_day)
                .order_by(Kline.date.asc())
                .all()
            )
            changed = False
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
