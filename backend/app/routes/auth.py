"""Auth routes — Phase 6 SMS flow + legacy single-password fallback.

Endpoints:
- POST /api/auth/sms/send       — request a verification code for a phone
- POST /api/auth/sms/verify     — verify code, upsert user, sign session
- POST /api/auth/login          — legacy single-password (still works
                                  while AUTH_DISABLED windows or for
                                  back-compat tests)
- POST /api/auth/logout         — clear cookie
- GET  /api/auth/me             — { user_id, phone } when v2 cookie,
                                  { ok: True } for legacy v1 sessions
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import (
    COOKIE_NAME, check_password, decode_token, issue_token, require_auth,
)
from ..db import get_db
from ..models import User
from ..services import sms

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


# --- SMS flow ------------------------------------------------------------


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

    # Upsert by phone — first verification creates the user; subsequent
    # logins just bump last_login_at + phone_verified_at.
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

    token = issue_token(user_id=user.id)
    response.set_cookie(
        key=COOKIE_NAME, value=token,
        httponly=True, samesite="lax",
        secure=False,  # set True behind HTTPS in production
        max_age=COOKIE_MAX_AGE, path="/",
    )
    return {"ok": True, "user_id": user.id, "phone": user.phone}


# --- Legacy single-password (kept for back-compat) -----------------------


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
def login(body: LoginRequest, response: Response):
    """Legacy single-password gate. Cookie issued has no user_id; routes
    that require user scoping will treat it as anonymous (effectively the
    same as AUTH_DISABLED). Kept so internal scripts and the old test
    flow still work while Phase 6 rolls out."""
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
    """Returns identity info to drive the frontend user chip / logout
    button. Returns 401 if no valid cookie. v1 cookies (no uid) come back
    with {ok: True, anonymous: True} so the UI can still render — they
    represent legacy single-password sessions."""
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
        # Cookie says you're user 42 but row is gone — treat as logged out.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return {"ok": True, "user_id": user.id, "phone": user.phone}
