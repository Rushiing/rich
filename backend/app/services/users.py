"""User-system bootstrapping helpers.

Two responsibilities:

1. `migrate_admin_watchlist()` runs at lifespan startup. Ensures the
   admin User row exists and backfills user_id on every NULL watchlist
   row. Idempotent.

2. `resolve_owner()` is a request-time helper used by all user-scoped
   routes. It folds three cases — real SMS-auth'd user, AUTH_DISABLED
   bypass, legacy v1 cookie — into one "the owner whose data we should
   read/write". When AUTH_DISABLED + ADMIN_PHONE is set, the admin
   becomes the implicit owner so dev mode acts like "logged in as
   admin". When neither user_id nor ADMIN_PHONE is available, returns
   None — routes interpret that as "show all rows" for back-compat.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal
from ..models import User, Watchlist
from ..services import sms

logger = logging.getLogger(__name__)


_admin_id_cache: int | None = None


def resolve_owner(user_id: int | None, db: Session) -> int | None:
    """Decide which user the current request acts on behalf of.

    - SMS-authed cookie carries a real `user_id` → return it directly.
    - AUTH_DISABLED bypass / legacy v1 cookie → `user_id` is None. Fall
      back to admin (resolved via ADMIN_PHONE) so dev/testing mode acts
      like the admin without needing the SMS dance every time.
    - No user_id AND no ADMIN_PHONE configured → return None. Caller
      treats this as "show all rows" for back-compat with the
      pre-account-system behaviour.

    The admin id is cached after first lookup; the User row never
    rotates so this is safe.
    """
    global _admin_id_cache
    if user_id is not None:
        return int(user_id)
    if not settings.ADMIN_PHONE:
        return None
    if _admin_id_cache is not None:
        return _admin_id_cache
    admin = db.query(User).filter(User.phone == settings.ADMIN_PHONE).first()
    if admin is None:
        return None
    _admin_id_cache = admin.id
    return admin.id


def migrate_admin_watchlist() -> dict:
    if not settings.ADMIN_PHONE:
        logger.info("admin migration: ADMIN_PHONE not set, skipping")
        return {"skipped": True}
    if not sms.is_valid_phone(settings.ADMIN_PHONE):
        logger.error(
            "admin migration: ADMIN_PHONE=%r is not a valid 11-digit "
            "Chinese mobile; skipping (fix env var to retry)",
            settings.ADMIN_PHONE,
        )
        return {"skipped": True, "error": "invalid phone"}

    db: Session = SessionLocal()
    try:
        admin = db.query(User).filter(User.phone == settings.ADMIN_PHONE).first()
        created = False
        if admin is None:
            admin = User(
                phone=settings.ADMIN_PHONE,
                # Treat the env-driven setup as "implicitly verified" —
                # otherwise the admin would have to SMS-log-in once before
                # owning their own watchlist after every redeploy.
                phone_verified_at=datetime.now(timezone.utc),
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)
            created = True
            logger.info("admin migration: created admin user id=%d phone=%s",
                        admin.id, admin.phone)

        # Backfill orphan watchlist rows.
        orphans = db.query(Watchlist).filter(Watchlist.user_id.is_(None)).all()
        if not orphans:
            logger.info("admin migration: no orphan watchlist rows (admin id=%d)",
                        admin.id)
            return {"admin_id": admin.id, "created": created, "claimed": 0}

        for row in orphans:
            row.user_id = admin.id
        db.commit()
        logger.info("admin migration: claimed %d watchlist rows for admin id=%d",
                    len(orphans), admin.id)
        return {"admin_id": admin.id, "created": created, "claimed": len(orphans)}
    except Exception:
        db.rollback()
        logger.exception("admin migration failed")
        return {"error": "see logs"}
    finally:
        db.close()
