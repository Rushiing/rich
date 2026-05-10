"""Phase 8: sector ranking + LLM-curated TOP-N picks."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..services import sectors as sectors_svc
from ..services import sector_picks as picks_svc

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


# 2-hour cache window for the LLM-curated picks. Long enough that we don't
# burn the LLM bill on light browsing, short enough that mid-day moves get
# refreshed if the user explicitly requests it.
PICKS_TTL_SECONDS = 2 * 60 * 60


class SectorPick(BaseModel):
    code: str
    name: str
    reason: str


class SectorPickGroup(BaseModel):
    name: str
    change_pct: float
    reason: str
    picks: list[SectorPick]


class SectorPicksResponse(BaseModel):
    sectors: list[SectorPickGroup]
    generated_at: str
    is_fresh: bool


@router.get("", response_model=list[Sector])
def list_sectors() -> list[dict[str, Any]]:
    """Returns 49 Sina-defined industries sorted by today's change_pct desc.
    Cached server-side for 5 min."""
    return sectors_svc.get_sectors()


@router.get("/picks", response_model=SectorPicksResponse)
def get_sector_picks(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Today's LLM-curated TOP-N sectors + per-sector picks. Cached 2h;
    request /picks/refresh to force regenerate."""
    try:
        return picks_svc.get_or_compute(db, max_age_seconds=PICKS_TTL_SECONDS)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"sector picks failed: {e}")


@router.post("/picks/refresh", response_model=SectorPicksResponse)
def refresh_sector_picks(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Force-regenerate today's recommendations. Same shape as GET; 0
    rate-limit for now since the cache layer + LLM cost are the natural
    backstop and we trust the small internal user pool."""
    try:
        return picks_svc.get_or_compute(db, max_age_seconds=0, force=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"sector picks refresh failed: {e}")
