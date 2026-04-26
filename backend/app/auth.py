"""Single-password auth for ~10 internal users (MVP).

Flow:
- Frontend POSTs the shared password to /api/auth/login.
- Backend compares to APP_PASSWORD, returns a signed token.
- Frontend stores it in an httpOnly cookie (set by Next.js route handler).
- Subsequent requests include the cookie; backend validates via require_auth.
"""
from __future__ import annotations

import hmac
from fastapi import Cookie, HTTPException, status
from itsdangerous import BadSignature, URLSafeSerializer

from .config import settings

COOKIE_NAME = "rich_session"
_serializer = URLSafeSerializer(settings.AUTH_SECRET, salt="rich-auth")


def issue_token() -> str:
    return _serializer.dumps({"v": 1})


def verify_token(token: str) -> bool:
    try:
        _serializer.loads(token)
        return True
    except BadSignature:
        return False


def check_password(password: str) -> bool:
    return hmac.compare_digest(password.encode(), settings.APP_PASSWORD.encode())


def require_auth(rich_session: str | None = Cookie(default=None)) -> None:
    if not rich_session or not verify_token(rich_session):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
