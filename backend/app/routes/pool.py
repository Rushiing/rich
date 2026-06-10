"""虚拟预选池 read API — B0/B1 (6/10).

Read-only for now: entries are created/promoted/eliminated exclusively by
the daily pool tick (services/virtual_pool.py). The "recommended" state
(human accepts a recommendable entry) is reserved for B3.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..services import virtual_pool as pool_svc

router = APIRouter(prefix="/api/pool", tags=["pool"], dependencies=[Depends(require_auth)])


@router.get("")
def get_pool(db: Session = Depends(get_db)):
    """Pool overview grouped by state: recommendable / observing /
    recently eliminated (+ counts). Shared across users — the pool is
    system state, not per-user."""
    return pool_svc.pool_overview(db)
