"""盯盘 view + manual snapshot trigger."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..models import Snapshot, Watchlist
from ..services.cron import run_snapshot_job
from ..services.signals import has_strong

router = APIRouter(prefix="/api/stocks", tags=["stocks"], dependencies=[Depends(require_auth)])


class StockRow(BaseModel):
    code: str
    name: str
    exchange: str
    last_ts: str | None
    price: float | None
    change_pct: float | None
    main_net_flow: float | None
    signals: list[str]
    has_strong_signal: bool
    news_count: int
    notices_count: int
    on_lhb: bool


@router.get("", response_model=list[StockRow])
def list_stocks(db: Session = Depends(get_db)):
    """Latest snapshot per watched code, joined with name/exchange from watchlist."""
    watch = {w.code: w for w in db.query(Watchlist).all()}
    if not watch:
        return []

    # Latest-per-code: subquery for max(id) per code, then join.
    subq = (
        db.query(Snapshot.code, Snapshot.id.label("id"))
        .order_by(Snapshot.code, desc(Snapshot.id))
        .distinct(Snapshot.code)
        .subquery()
    )
    # The DISTINCT ON above is Postgres-only. Fallback for SQLite (smoke tests):
    # query latest per code via a dialect-agnostic approach.
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        latest_ids: list[int] = []
        for code in watch.keys():
            r = (
                db.query(Snapshot.id)
                .filter(Snapshot.code == code)
                .order_by(desc(Snapshot.id))
                .first()
            )
            if r:
                latest_ids.append(r[0])
        snaps = db.query(Snapshot).filter(Snapshot.id.in_(latest_ids)).all() if latest_ids else []
    else:
        snaps = db.query(Snapshot).join(subq, Snapshot.id == subq.c.id).all()

    by_code = {s.code: s for s in snaps}

    rows: list[StockRow] = []
    for code, w in watch.items():
        s = by_code.get(code)
        signals = (s.signals if s else None) or []
        rows.append(StockRow(
            code=code,
            name=w.name,
            exchange=w.exchange,
            last_ts=s.ts.isoformat() if s else None,
            price=(s.price if s else None),
            change_pct=(s.change_pct if s else None),
            main_net_flow=(s.main_net_flow if s else None),
            signals=signals,
            has_strong_signal=has_strong(signals),
            news_count=len(s.news) if s and s.news else 0,
            notices_count=len(s.notices) if s and s.notices else 0,
            on_lhb=bool(s.lhb) if s else False,
        ))
    # Strong-signal rows first, then by absolute change desc
    rows.sort(key=lambda r: (
        not r.has_strong_signal,
        -abs(r.change_pct or 0),
    ))
    return rows


class SnapshotResult(BaseModel):
    codes: int
    inserted: int
    post_close: bool = False


@router.post("/snapshot", response_model=SnapshotResult)
def trigger_snapshot(post_close: bool = False):
    """Synchronously run the snapshot job. Useful for testing in production
    without waiting for the cron tick."""
    try:
        return SnapshotResult(**run_snapshot_job(post_close=post_close))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


class StockDetail(BaseModel):
    code: str
    name: str
    exchange: str
    last_ts: str | None
    price: float | None
    change_pct: float | None
    main_net_flow: float | None
    signals: list[str]
    news: list[dict[str, Any]]
    notices: list[dict[str, Any]]
    lhb: dict[str, Any] | None


@router.get("/{code}", response_model=StockDetail)
def stock_detail(code: str, db: Session = Depends(get_db)):
    """Latest snapshot detail for one stock (used by Phase 3 deep-analysis page header)."""
    w = db.query(Watchlist).filter(Watchlist.code == code).first()
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")
    s = (
        db.query(Snapshot)
        .filter(Snapshot.code == code)
        .order_by(desc(Snapshot.id))
        .first()
    )
    return StockDetail(
        code=code,
        name=w.name,
        exchange=w.exchange,
        last_ts=s.ts.isoformat() if s else None,
        price=(s.price if s else None),
        change_pct=(s.change_pct if s else None),
        main_net_flow=(s.main_net_flow if s else None),
        signals=(s.signals if s else None) or [],
        news=(s.news if s else None) or [],
        notices=(s.notices if s else None) or [],
        lhb=(s.lhb if s else None),
    )
