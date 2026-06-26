"""Per-user auth for the rich app(邀请码 + 手机号 + 密码)。

Cookie payload `{"v": 2, "uid": int}` 携带认证用户 id。老的 v1 cookie(单一共享
密码时代,payload `{"v": 1}`、无 uid)读时仍接受,但 surface 成 `user_id=None`
→ require_auth 在用户态路由上转成 401(除非 AUTH_DISABLED 这个 dev 绕过开着)。

6/26:SMS 验证 + 单一共享密码登录已移除 —— 认证纯 per-user 手机号+密码,注册靠
邀请码。
"""
from __future__ import annotations

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


def require_auth(rich_session: str | None = Cookie(default=None)) -> int | None:
    """Gate for protected routes. Returns the authenticated user_id (int)
    or None for legacy/anonymous sessions.

    Resolution order matters — a valid cookie ALWAYS beats AUTH_DISABLED:
    - Cookie v2 (has uid) → returns the uid. This is the SMS-authed path
      and must take precedence over AUTH_DISABLED so users B/C don't get
      bucketed back to the admin's watchlist when AUTH_DISABLED is on.
    - Cookie v1 (no uid) → None. Routes treat this as anonymous; combined
      with ADMIN_PHONE set, resolve_owner() folds it into the admin user.
    - No cookie + AUTH_DISABLED → None (bypass). Same anonymous path as v1.
    - No cookie + AUTH_DISABLED off → 401.
    - Cookie present but unsigned/tampered → 401.
    """
    if rich_session:
        payload = decode_token(rich_session)
        if payload is None:
            # Cookie present but tampered with — fail closed even when
            # AUTH_DISABLED. Real users with broken cookies should re-login.
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        uid = payload.get("uid")
        if isinstance(uid, int):
            return int(uid)
        # v1 payload (no uid) — fall through to anonymous handling below.
    if settings.AUTH_DISABLED:
        return None
    if not rich_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    # Cookie present but v1-shaped while AUTH_DISABLED is off. codex 安全审计
    # P1:不要再把 v1 当匿名(返回 None)—— 那会让用户态路由经 resolve_owner 的
    # owner=None 全局兜底(ADMIN_PHONE 漏配时)读/写全局 watchlist。单密码时代
    # 已结束,强制重新登录拿 v2(带 uid)cookie。于是 AUTH_DISABLED 关掉后
    # require_auth 只会返回真实 uid 或 401,绝不返回 None → 用户态路由无全局入口。
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
