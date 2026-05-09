"""Phase 8: sector ranking page."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth import require_auth
from ..services import sectors as sectors_svc

router = APIRouter(prefix="/api/sectors", tags=["sectors"], dependencies=[Depends(require_auth)])


class SectorLeader(BaseModel):
    code: str
    name: str
    change_pct: float
    price: float | None


class Sector(BaseModel):
    name: str
    code: str
    company_count: int
    avg_price: float | None
    change_pct: float
    total_volume: float | None
    total_turnover: float | None
    leader: SectorLeader


@router.get("", response_model=list[Sector])
def list_sectors() -> list[dict[str, Any]]:
    """Returns 49 Sina-defined industries sorted by today's change_pct desc.
    Cached server-side for 5 min."""
    return sectors_svc.get_sectors()
