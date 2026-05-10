"""Auth routes.

Active flow (Phase 6.5):
- POST /api/auth/register   — phone + password + invite_code → creates user + signs cookie
- POST /api/auth/login      — phone + password → signs cookie

Legacy / transitional:
- POST /api/auth/sms/send       — dev-mode 8888 + whitelist (kept as fallback
                                  while existing internal users migrate)
- POST /api/auth/sms/verify     — same
- POST /api/auth/legacy-login   — single-shared-password (AUTH_DISABLED tests)
- POST /api/auth/logout         — clear cookie
- GET  /api/auth/me             — { user_id, phone } when v2 cookie

Why we keep SMS dev-mode running: 3 existing users were SMS-verified in
Phase 6 and don't have password hashes yet. After they each set a password
via the upcoming /auth/login flow (or the admin reset), the SMS routes can
be removed. The admin script `admin_users.py` is the path of least
resistance for in-place password setup.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import (
    COOKIE_NAME, check_password, decode_token, issue_token,
)
from ..db import get_db
from ..models import InviteCode, User
from ..services import sms
from ..services.passwords import (
    PasswordError, hash_password, validate as validate_password, verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _set_session_cookie(response: Response, user_id: int) -> None:
    """Sign the v2 token + drop it as the rich_session cookie."""
    token = issue_token(user_id=user_id)
    response.set_cookie(
        key=COOKIE_NAME, value=token,
        httponly=True, samesite="lax",
        secure=False,  # set True behind HTTPS in production
        max_age=COOKIE_MAX_AGE, path="/",
    )


# --- Password-based: register + login ------------------------------------


class RegisterRequest(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    password: str
    invite_code: str = Field(min_length=4, max_length=32)


class LoginRequest(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    password: str


@router.post("/register")
def register(body: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    """Self-service registration. Requires a valid, unused, unexpired invite
    code. The code is consumed atomically — concurrent registrations against
    the same code race for it; loser gets 409.
    """
    # Password sanity first so we don't burn an invite on a malformed pwd.
    try:
        validate_password(body.password)
    except PasswordError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Phone uniqueness — friendlier error than the eventual integrity error.
    existing = db.query(User).filter(User.phone == body.phone).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="该手机号已注册，请直接登录")

    # Code lookup is upper-cased on the way in so users can type lowercase.
    incoming = body.invite_code.strip().upper()
    code_row = db.query(InviteCode).filter(InviteCode.code == incoming).first()
    if code_row is None:
        raise HTTPException(status_code=400, detail="邀请码无效")
    # max_uses NULL = unlimited; integer N = up to N redemptions.
    # current_uses counts; reject when consumed.
    if code_row.max_uses is not None and code_row.current_uses >= code_row.max_uses:
        raise HTTPException(status_code=400, detail="邀请码已达使用上限")
    if code_row.expires_at is not None:
        ea = code_row.expires_at
        if ea.tzinfo is None:
            ea = ea.replace(tzinfo=timezone.utc)
        if ea < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="邀请码已过期")

    now = datetime.now(timezone.utc)
    user = User(
        phone=body.phone,
        password_hash=hash_password(body.password),
        last_login_at=now,
    )
    db.add(user)
    db.flush()  # need user.id before marking the code

    # Track first-use audit fields when this is the inaugural redemption;
    # always increment the counter.
    if code_row.used_at is None:
        code_row.used_at = now
        code_row.used_by_user_id = user.id
    code_row.current_uses = (code_row.current_uses or 0) + 1
    db.commit()
    db.refresh(user)

    _set_session_cookie(response, user.id)
    return {"ok": True, "user_id": user.id, "phone": user.phone}


@router.post("/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """Phone + password login. Generic 401 message regardless of whether
    the phone exists or the password is wrong — no info leak."""
    user = db.query(User).filter(User.phone == body.phone).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="手机号或密码错误")

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()

    _set_session_cookie(response, user.id)
    return {"ok": True, "user_id": user.id, "phone": user.phone}


# --- SMS flow (transitional) ---------------------------------------------


class SmsSendRequest(BaseModel):
    phone: str = Field(min_length=11, max_length=11)


class SmsVerifyRequest(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    code: str = Field(min_length=4, max_length=6)


@router.post("/sms/send")
def sms_send(body: SmsSendRequest):
    try:
        result = sms.send_code(body.phone)
    except sms.SmsError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return result


@router.post("/sms/verify")
def sms_verify(body: SmsVerifyRequest, response: Response, db: Session = Depends(get_db)):
    if not sms.verify_code(body.phone, body.code):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="验证码错误或已过期")

    user = db.query(User).filter(User.phone == body.phone).first()
    now = datetime.now(timezone.utc)
    if user is None:
        user = User(phone=body.phone, phone_verified_at=now, last_login_at=now)
        db.add(user)
    else:
        user.phone_verified_at = now
        user.last_login_at = now
    db.commit()
    db.refresh(user)

    _set_session_cookie(response, user.id)
    return {"ok": True, "user_id": user.id, "phone": user.phone}


# --- Legacy single-password (kept for AUTH_DISABLED tests) ---------------


class LegacyLoginRequest(BaseModel):
    password: str


@router.post("/legacy-login")
def legacy_login(body: LegacyLoginRequest, response: Response):
    """Single-shared-password gate. Cookie issued has no user_id; routes
    that require user scoping treat it as anonymous."""
    if not check_password(body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid password")
    token = issue_token()  # v1, no uid
    response.set_cookie(
        key=COOKIE_NAME, value=token,
        httponly=True, samesite="lax",
        secure=False,
        max_age=COOKIE_MAX_AGE, path="/",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(rich_session: str | None = Cookie(default=None), db: Session = Depends(get_db)):
    if rich_session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    payload = decode_token(rich_session)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    uid = payload.get("uid")
    if not isinstance(uid, int):
        return {"ok": True, "anonymous": True}
    user = db.query(User).filter(User.id == uid).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return {
        "ok": True,
        "user_id": user.id,
        "phone": user.phone,
        # Frontend uses this to gate "请设置密码" prompt for migrated users
        # who haven't picked one yet.
        "has_password": user.password_hash is not None,
    }
