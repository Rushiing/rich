"""盯盘 view + manual snapshot trigger."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..models import Analysis, Snapshot, Watchlist
from ..services.analysis import generate as analysis_generate, get_cached as analysis_cached
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


# --- Phase 3: deep analysis ----------------------------------------------


class AnalysisOut(BaseModel):
    code: str
    key_table: dict[str, Any]
    deep_analysis: str
    model: str
    strategy: str
    created_at: str
    snapshot_id: int | None
    is_fresh: bool

    @classmethod
    def from_row(cls, row: Analysis, is_fresh: bool) -> "AnalysisOut":
        return cls(
            code=row.code,
            key_table=row.key_table,
            deep_analysis=row.deep_analysis,
            model=row.model,
            strategy=row.strategy,
            created_at=row.created_at.isoformat() if row.created_at else "",
            snapshot_id=row.snapshot_id,
            is_fresh=is_fresh,
        )


@router.get("/{code}/analysis", response_model=AnalysisOut | None)
def get_analysis(code: str, db: Session = Depends(get_db)):
    """Return cached analysis if it exists and is < 4h old. Returns null otherwise.

    Frontend reads this on page load; if null it shows a "生成" CTA.
    """
    row = db.query(Analysis).filter(Analysis.code == code).first()
    if row is None:
        return None
    fresh = analysis_cached(db, code) is not None
    return AnalysisOut.from_row(row, is_fresh=fresh)


@router.post("/{code}/analysis", response_model=AnalysisOut)
def generate_analysis(code: str, db: Session = Depends(get_db)):
    """Force regenerate. Returns the new row."""
    if not db.query(Watchlist).filter(Watchlist.code == code).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")
    try:
        row = analysis_generate(db, code)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return AnalysisOut.from_row(row, is_fresh=True)
