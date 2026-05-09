"""SMS-based auth for the rich app (Phase 6).

Migration history:
- v1 (Phase 0): single shared APP_PASSWORD; cookie payload `{"v": 1}`
- v2 (Phase 6): per-user via SMS verification; cookie payload
    `{"v": 2, "uid": int}` carries the authenticated user id

Both schemas are accepted on read so an in-flight cookie from the v1 era
(if AUTH_DISABLED was on or someone still has a single-password session)
keeps working through the rollout window. v1 cookies surface as
`user_id=None`, which require_auth turns into a 401 on user-scoped routes
unless AUTH_DISABLED is set.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Cookie, HTTPException, status
from itsdangerous import BadSignature, URLSafeSerializer

from .config import settings

COOKIE_NAME = "rich_session"
_serializer = URLSafeSerializer(settings.AUTH_SECRET, salt="rich-auth")


def issue_token(user_id: Optional[int] = None) -> str:
    """Sign a session token. Pass user_id for SMS-verified sessions; omit
    for legacy single-password sessions (returns a v1-shaped payload that
    older code paths still verify)."""
    if user_id is None:
        return _serializer.dumps({"v": 1})
    return _serializer.dumps({"v": 2, "uid": int(user_id)})


def verify_token(token: str) -> bool:
    """Cheap validity check. Used by middleware for redirect logic — does
    not surface user_id. Routes that need user_id should rely on
    `require_auth` (which decodes the same token and pulls uid out)."""
    try:
        _serializer.loads(token)
        return True
    except BadSignature:
        return False


def decode_token(token: str) -> dict | None:
    """Decode and validate; returns the payload or None on bad signature."""
    try:
        payload = _serializer.loads(token)
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def check_password(password: str) -> bool:
    """Single-password legacy check. Retained so AUTH_DISABLED-bypass and
    the legacy /api/auth/login endpoint still work without DB writes."""
    return hmac.compare_digest(password.encode(), settings.APP_PASSWORD.encode())


def require_auth(rich_session: str | None = Cookie(default=None)) -> int | None:
    """Gate for protected routes. Returns the authenticated user_id (int)
    or None when running with AUTH_DISABLED.

    - AUTH_DISABLED=True → returns None (caller decides whether to apply
      a default user_id; today most routes treat None as "all users".
      That's the legacy behaviour and still wanted in dev/test mode.)
    - Cookie missing or unsigned → 401
    - Cookie v1 (no uid) → returns None; user-scoped routes will treat
      the request as if AUTH_DISABLED. Phase 6 forces real users to log
      out + log back in via SMS to upgrade their cookie to v2.
    - Cookie v2 → returns the embedded uid as int.
    """
    if settings.AUTH_DISABLED:
        return None
    if not rich_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    payload = decode_token(rich_session)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    uid = payload.get("uid")
    return int(uid) if isinstance(uid, int) else None
