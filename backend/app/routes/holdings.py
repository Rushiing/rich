"""Per-user holdings (cost basis) CRUD.

A holding records what a user actually owns: cost price + share count.
The detail page renders a "持仓对照" overlay computed from the holding +
the (globally cached) analysis numbers — so cost-basis personalization
doesn't multiply LLM spend or fork the shared analysis cache.

Endpoints (all scoped to the authenticated user):
- GET    /api/holdings            list all holdings
- GET    /api/holdings/{code}     single holding (404 if none)
- PUT    /api/holdings/{code}     upsert cost_price / shares / opened_at / note
- DELETE /api/holdings/{code}     remove
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..models import Holding
from ..services.users import resolve_owner

router = APIRouter(prefix="/api/holdings", tags=["holdings"])


class HoldingItem(BaseModel):
    code: str
    cost_price: float
    shares: float | None
    opened_at: str | None
    note: str | None
    updated_at: str

    @classmethod
    def from_row(cls, row: Holding) -> "HoldingItem":
        return cls(
            code=row.code,
            cost_price=row.cost_price,
            shares=row.shares,
            opened_at=row.opened_at,
            note=row.note,
            updated_at=row.updated_at.isoformat() if row.updated_at else "",
        )


class HoldingUpsert(BaseModel):
    cost_price: float = Field(gt=0, description="买入均价，必须 > 0")
    shares: float | None = Field(default=None, ge=0)
    opened_at: str | None = Field(default=None, max_length=10)
    note: str | None = Field(default=None, max_length=100)


def _require_owner(user_id: int | None, db: Session) -> int:
    """Holdings are inherently per-user. Unlike watchlist (which tolerates
    a None owner in legacy mode), a holding with no owner is meaningless —
    fail clearly."""
    owner = resolve_owner(user_id, db)
    if owner is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="持仓功能需要登录账号",
        )
    return owner


@router.get("", response_model=list[HoldingItem])
def list_holdings(
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    owner = _require_owner(user_id, db)
    rows = db.query(Holding).filter(Holding.user_id == owner).all()
    return [HoldingItem.from_row(r) for r in rows]


@router.get("/{code}", response_model=HoldingItem | None)
def get_holding(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Returns the holding for this code, or null when the user doesn't
    hold it. null (not 404) keeps the detail-page fetch simple."""
    owner = _require_owner(user_id, db)
    row = (
        db.query(Holding)
        .filter(Holding.user_id == owner, Holding.code == code)
        .first()
    )
    return HoldingItem.from_row(row) if row else None


@router.put("/{code}", response_model=HoldingItem)
def upsert_holding(
    code: str,
    body: HoldingUpsert,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    owner = _require_owner(user_id, db)
    now = datetime.now(timezone.utc)
    row = (
        db.query(Holding)
        .filter(Holding.user_id == owner, Holding.code == code)
        .first()
    )
    if row is None:
        row = Holding(
            user_id=owner, code=code,
            cost_price=body.cost_price, shares=body.shares,
            opened_at=body.opened_at, note=body.note,
        )
        db.add(row)
    else:
        row.cost_price = body.cost_price
        row.shares = body.shares
        row.opened_at = body.opened_at
        row.note = body.note
        row.updated_at = now
    db.commit()
    db.refresh(row)
    return HoldingItem.from_row(row)


@router.delete("/{code}")
def delete_holding(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    owner = _require_owner(user_id, db)
    row = (
        db.query(Holding)
        .filter(Holding.user_id == owner, Holding.code == code)
        .first()
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    db.delete(row)
    db.commit()
    return {"ok": True}
