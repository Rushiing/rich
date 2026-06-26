"""auth happy-path smoke —— register + login **成功路径**。

这就是 6/26 漏检炸过的那条网:_set_session_cookie 用了 settings.COOKIE_SECURE
但 auth.py 没 import settings → 登录成功验密后、发 cookie 那步 500。当初安全
验证只测了拒绝路径(401),没测成功路径(200 + Set-Cookie),正好漏过。

直接调路由函数 + 内存 sqlite + 真 Response,断言 200 且 Set-Cookie 头生成。
跑法:cd backend && .venv/bin/python tests/test_auth_smoke.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException, Response
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import InviteCode, User
from app.routes.auth import LoginRequest, RegisterRequest, login, register
from app.services.passwords import hash_password

PHONE = "13800138000"
PWD = "test-password-123"


def _db():
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)()


def test_register_then_login_set_cookie():
    db = _db()
    db.add(InviteCode(code="TESTCODE", current_uses=0, max_uses=None))
    db.commit()

    # register(邀请码 + 手机号 + 密码)
    r1 = Response()
    out1 = register(
        RegisterRequest(phone=PHONE, password=PWD, invite_code="testcode"), r1, db,
    )
    assert out1["ok"] and out1["phone"] == PHONE
    assert r1.headers.get("set-cookie"), "register 必须 Set-Cookie(6/26 这步 NameError 炸过)"

    # login(手机号 + 密码)
    r2 = Response()
    out2 = login(LoginRequest(phone=PHONE, password=PWD), r2, db)
    assert out2["ok"] and out2["user_id"] == out1["user_id"]
    assert r2.headers.get("set-cookie"), "login 必须 Set-Cookie"
    print("✓ register + login 成功路径 Set-Cookie")


def test_login_wrong_password_401():
    db = _db()
    db.add(User(phone=PHONE, password_hash=hash_password(PWD)))
    db.commit()
    r = Response()
    try:
        login(LoginRequest(phone=PHONE, password="totally-wrong-9"), r, db)
        assert False, "错密码应 401"
    except HTTPException as e:
        assert e.status_code == 401
    print("✓ 错密码 → 401")


def test_register_bad_invite_400():
    db = _db()
    r = Response()
    try:
        register(RegisterRequest(phone=PHONE, password=PWD, invite_code="NOPE"), r, db)
        assert False, "无效邀请码应 400"
    except HTTPException as e:
        assert e.status_code == 400
    print("✓ 无效邀请码 → 400")


if __name__ == "__main__":
    test_register_then_login_set_cookie()
    test_login_wrong_password_401()
    test_register_bad_invite_400()
    print("\nALL PASS")
