"""Market-wide endpoints for the dashboard."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth import require_auth
from ..services import market as market_svc

router = APIRouter(prefix="/api/market", tags=["market"], dependencies=[Depends(require_auth)])


class IndexQuote(BaseModel):
    symbol: str
    name: str
    point: float
    change_pct: float


@router.get("/indices", response_model=list[IndexQuote])
def list_indices() -> list[dict[str, Any]]:
    """上证 / 深证 / 创业板 当日点位 + 涨跌幅. Cached 60s server-side."""
    return market_svc.get_indices()
