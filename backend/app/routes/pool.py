"""虚拟预选池 read API — B0/B1 (6/10).

Read-only for now: entries are created/promoted/eliminated exclusively by
the daily pool tick (services/virtual_pool.py). The "recommended" state
(human accepts a recommendable entry) is reserved for B3.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..models import Analysis, PoolEntry, User
from ..services import virtual_pool as pool_svc
from ..services.users import resolve_owner

router = APIRouter(prefix="/api/pool", tags=["pool"], dependencies=[Depends(require_auth)])


@router.get("")
def get_pool(db: Session = Depends(get_db)):
    """Pool overview grouped by state: recommendable / observing /
    recently eliminated (+ counts). Shared across users — the pool is
    system state, not per-user."""
    return pool_svc.pool_overview(db)


@router.get("/my-sectors")
def get_my_sectors(
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """The current user's 关注板块 prefs + the full pickable theme list.

    available = the designated-channel priority themes (the curated set
    that's guaranteed to enter the pool). selected = this user's saved
    picks. Display-layer only — does not change what enters the pool.
    """
    available = [s["theme"] for s in pool_svc.PRIORITY_SECTORS]
    owner = resolve_owner(user_id, db)
    selected: list[str] = []
    if owner is not None:
        u = db.query(User).filter(User.id == owner).first()
        if u is not None and u.preferred_sectors:
            selected = list(u.preferred_sectors)
    return {"available": available, "selected": selected}


class MySectorsUpdate(BaseModel):
    sectors: list[str]


@router.put("/my-sectors")
def set_my_sectors(
    body: MySectorsUpdate,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Save the user's 关注板块 picks. Silently drops anything not in the
    available theme list so a stale client can't write garbage."""
    owner = resolve_owner(user_id, db)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="关注板块需要登录账号")
    valid = {s["theme"] for s in pool_svc.PRIORITY_SECTORS}
    cleaned = [s for s in body.sectors if s in valid]
    u = db.query(User).filter(User.id == owner).first()
    if u is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    u.preferred_sectors = cleaned
    db.commit()
    return {"selected": cleaned}


@router.get("/{code}")
def get_pool_detail(code: str, db: Session = Depends(get_db)):
    """预选池专属详情(区域分离:**不碰** /api/stocks 那套自选语义)。

    返回 {entry, analysis}:
      - entry: 该 code 最近一条 pool 记录(活跃优先) — 入池价/收益/回撤/
        观察天数/cohort/thesis 证据 + 失效线
      - analysis: 晋升时挂的深度解析(Analysis 表,复用 AnalysisOut 结构,
        前端能直接喂 KeyTableCard/DeepAnalysis 那套展示组件)。没解析 → None。
    """
    # 该 code 最近一条 pool entry(活跃 observing/recommendable 优先于已淘汰)
    e = (
        db.query(PoolEntry)
        .filter(PoolEntry.code == code)
        .order_by(
            PoolEntry.state.in_(("observing", "recommendable")).desc(),
            desc(PoolEntry.entered_at),
        )
        .first()
    )
    if e is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in pool")

    # 深度解析(复用 stocks 的 AnalysisOut 序列化,但走 pool endpoint —
    # 数据复用、边界独立)。
    from .stocks import AnalysisOut
    a = db.query(Analysis).filter(Analysis.code == code).first()
    analysis = AnalysisOut.from_row(a, is_fresh=True) if a is not None else None

    return {
        "entry": _entry_dict(e),
        "analysis": analysis,
    }


def _entry_dict(e: PoolEntry) -> dict:
    """同 pool_overview._row 的字段(复用语义,内联避免把内部闭包 export)。"""
    return {
        "id": e.id, "code": e.code, "name": e.name, "source": e.source,
        "state": e.state,
        "entered_at": e.entered_at.isoformat() if e.entered_at else None,
        "entry_date": e.entry_date, "entry_close": e.entry_close,
        "last_close": e.last_close, "last_date": e.last_date,
        "return_pct": round(e.return_pct, 2) if e.return_pct is not None else None,
        "max_drawdown_pct": round(e.max_drawdown_pct, 2) if e.max_drawdown_pct is not None else None,
        "days_observed": e.days_observed, "thesis": e.thesis,
        "eliminated_reason": e.eliminated_reason, "cohort_week": e.cohort_week,
        "state_changed_at": e.state_changed_at.isoformat() if e.state_changed_at else None,
    }
