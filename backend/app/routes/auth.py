from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from ..auth import COOKIE_NAME, check_password, issue_token, require_auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
def login(body: LoginRequest, response: Response):
    if not check_password(body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid password")
    token = issue_token()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,  # set True behind HTTPS in production
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(_: None = Depends(require_auth)):
    return {"ok": True}
