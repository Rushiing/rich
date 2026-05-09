from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import require_auth
from ..db import get_db
from ..models import Watchlist
from ..services.stocks import lookup_codes, normalize_codes
from ..services.users import resolve_owner

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchlistItem(BaseModel):
    code: str
    name: str
    exchange: str
    added_at: str
    starred: bool = False

    @classmethod
    def from_row(cls, row: Watchlist) -> "WatchlistItem":
        return cls(
            code=row.code,
            name=row.name,
            exchange=row.exchange,
            added_at=row.added_at.isoformat() if row.added_at else "",
            starred=bool(getattr(row, "starred", False)),
        )


class ImportRequest(BaseModel):
    codes: list[str] = Field(default_factory=list)
    raw: str | None = None  # alternative: pass a free-form blob


class ImportResult(BaseModel):
    added: list[WatchlistItem]
    skipped_existing: list[str]
    invalid_format: list[str]  # ^\d{6}$ check failed
    lookup_failed: list[str]   # format ok but akshare returned no name — retryable


def _scoped_query(db: Session, owner: int | None):
    """Apply user-scope filter to a Watchlist query. None means
    'pre-account-system mode' — return all rows so legacy single-password
    sessions and AUTH_DISABLED with no admin still see the data."""
    q = db.query(Watchlist)
    if owner is not None:
        q = q.filter(Watchlist.user_id == owner)
    return q


@router.get("", response_model=list[WatchlistItem])
def list_watchlist(
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    owner = resolve_owner(user_id, db)
    rows = _scoped_query(db, owner).order_by(Watchlist.added_at.desc()).all()
    return [WatchlistItem.from_row(r) for r in rows]


@router.post("/import", response_model=ImportResult)
def import_codes(
    body: ImportRequest,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    owner = resolve_owner(user_id, db)

    # Normalize inputs from either explicit list or free-form blob
    incoming: list[str] = []
    if body.raw:
        incoming.extend(normalize_codes(body.raw))
    incoming.extend(normalize_codes(" ".join(body.codes)))
    seen: set[str] = set()
    deduped = []
    for c in incoming:
        if c not in seen:
            seen.add(c)
            deduped.append(c)

    if not deduped:
        return ImportResult(added=[], skipped_existing=[], invalid_format=[], lookup_failed=[])

    resolved = lookup_codes(deduped)

    invalid_format = [c for c, v in resolved.items() if v == "invalid_format"]
    lookup_failed = [c for c, v in resolved.items() if v == "lookup_failed"]
    valid = {c: v for c, v in resolved.items() if isinstance(v, dict)}

    # "Already in this user's watchlist" check — scoped to the current
    # owner. A different user owning the same code should not block.
    existing_codes = {
        r.code
        for r in _scoped_query(db, owner)
                  .with_entities(Watchlist.code)
                  .filter(Watchlist.code.in_(list(valid.keys())))
                  .all()
    }

    added: list[WatchlistItem] = []
    for code, info in valid.items():
        if code in existing_codes:
            continue
        # NOTE: with `code` still as PK (rollout phase) we cannot insert
        # the same code twice across users. Once a second user starts
        # adding existing codes we'll need the PK swap follow-up. For now
        # this branch is unreachable for non-admin users since admin
        # already owns all 61 historical rows.
        row = Watchlist(
            code=info["code"], name=info["name"], exchange=info["exchange"],
            user_id=owner,
        )
        db.add(row)
        db.flush()
        added.append(WatchlistItem.from_row(row))
    db.commit()

    return ImportResult(
        added=added,
        skipped_existing=sorted(existing_codes),
        invalid_format=invalid_format,
        lookup_failed=lookup_failed,
    )


@router.delete("/{code}")
def delete_one(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    owner = resolve_owner(user_id, db)
    row = _scoped_query(db, owner).filter(Watchlist.code == code).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    db.delete(row)
    db.commit()
    return {"ok": True}


class BulkDeleteRequest(BaseModel):
    codes: list[str] = Field(default_factory=list)
    raw: str | None = None  # accept paste-style input like /import


class BulkDeleteResult(BaseModel):
    deleted: list[str]
    not_found: list[str]


@router.post("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete(
    body: BulkDeleteRequest,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Delete multiple codes at once. Mirrors /import's input shape so
    the frontend can reuse the paste textarea + Excel/CSV parsing.

    Codes not in the user's watchlist quietly land in `not_found` instead
    of failing the whole batch — same forgiving behaviour as /import."""
    owner = resolve_owner(user_id, db)
    incoming: list[str] = []
    if body.raw:
        incoming.extend(normalize_codes(body.raw))
    incoming.extend(normalize_codes(" ".join(body.codes)))
    seen: set[str] = set()
    deduped = [c for c in incoming if not (c in seen or seen.add(c))]
    if not deduped:
        return BulkDeleteResult(deleted=[], not_found=[])

    rows = (
        _scoped_query(db, owner)
        .filter(Watchlist.code.in_(deduped))
        .all()
    )
    found = {r.code for r in rows}
    for r in rows:
        db.delete(r)
    db.commit()
    return BulkDeleteResult(
        deleted=sorted(found),
        not_found=sorted(c for c in deduped if c not in found),
    )


class StarToggleResult(BaseModel):
    code: str
    starred: bool


@router.post("/{code}/star", response_model=StarToggleResult)
def toggle_star(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Flip the starred flag for one code, scoped to the current owner so
    user A can't star user B's row. Returns the new state."""
    owner = resolve_owner(user_id, db)
    row = _scoped_query(db, owner).filter(Watchlist.code == code).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    row.starred = not bool(row.starred)
    db.commit()
    return StarToggleResult(code=code, starred=row.starred)
