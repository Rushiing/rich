from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..models import Watchlist
from ..services.stocks import lookup_codes, normalize_codes

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"], dependencies=[Depends(require_auth)])


class WatchlistItem(BaseModel):
    code: str
    name: str
    exchange: str
    added_at: str

    @classmethod
    def from_row(cls, row: Watchlist) -> "WatchlistItem":
        return cls(
            code=row.code,
            name=row.name,
            exchange=row.exchange,
            added_at=row.added_at.isoformat() if row.added_at else "",
        )


class ImportRequest(BaseModel):
    codes: list[str] = Field(default_factory=list)
    raw: str | None = None  # alternative: pass a free-form blob


class ImportResult(BaseModel):
    added: list[WatchlistItem]
    skipped_existing: list[str]
    invalid: list[str]


@router.get("", response_model=list[WatchlistItem])
def list_watchlist(db: Session = Depends(get_db)):
    rows = db.query(Watchlist).order_by(Watchlist.added_at.desc()).all()
    return [WatchlistItem.from_row(r) for r in rows]


@router.post("/import", response_model=ImportResult)
def import_codes(body: ImportRequest, db: Session = Depends(get_db)):
    # Normalize inputs from either explicit list or free-form blob
    incoming: list[str] = []
    if body.raw:
        incoming.extend(normalize_codes(body.raw))
    incoming.extend(normalize_codes(" ".join(body.codes)))
    # dedupe preserving order
    seen: set[str] = set()
    deduped = []
    for c in incoming:
        if c not in seen:
            seen.add(c)
            deduped.append(c)

    if not deduped:
        return ImportResult(added=[], skipped_existing=[], invalid=[])

    resolved = lookup_codes(deduped)

    invalid = [c for c, v in resolved.items() if v is None]
    valid = {c: v for c, v in resolved.items() if v is not None}

    existing_codes = {
        r.code
        for r in db.query(Watchlist.code).filter(Watchlist.code.in_(list(valid.keys()))).all()
    }

    added: list[WatchlistItem] = []
    for code, info in valid.items():
        if code in existing_codes:
            continue
        row = Watchlist(code=info["code"], name=info["name"], exchange=info["exchange"])
        db.add(row)
        db.flush()
        added.append(WatchlistItem.from_row(row))
    db.commit()

    return ImportResult(
        added=added,
        skipped_existing=sorted(existing_codes),
        invalid=invalid,
    )


@router.delete("/{code}")
def delete_one(code: str, db: Session = Depends(get_db)):
    row = db.query(Watchlist).filter(Watchlist.code == code).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    db.delete(row)
    db.commit()
    return {"ok": True}
