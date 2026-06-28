"""详情页生成/重新生成异步化单测(6/28)。

背景:慢网关单次解析 ~50s,超过 Railway ~30s HTTP 代理上限,旧同步 POST
返回 "Failed to fetch"。改成:POST 起后台线程立即返回 started=True,前端轮询
GET /{code}/analysis/status,running 翻 false 后再拉缓存行。

这里直接调路由函数 + 内存 sqlite,把 analysis_generate 打桩(不打真 LLM),
断言:启动→后台跑→状态可查→错误能透出。

跑法:cd backend && .venv/bin/python tests/test_analysis_async.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.routes.stocks as stocks
from app.db import Base
from app.models import Watchlist


def _db():
    e = create_engine("sqlite://")
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)()


def _wait_done(code: str, timeout_s: float = 5.0):
    """轮 status 直到 running=False(模拟前端 poll)。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = stocks.single_analysis_status(code)
        if not st.running:
            return st
        time.sleep(0.02)
    raise AssertionError("background analysis did not finish in time")


def test_async_start_then_succeeds(monkeypatch):
    db = _db()
    db.add(Watchlist(id=1, code="600000", user_id=None, name="浦发银行", exchange="sh"))
    db.commit()

    calls: list[tuple] = []

    # 桩:记录调用参数,模拟一次成功的解析(不碰 LLM / 不写 Analysis 行)。
    def fake_generate(_db, code, *, mode="single", force=False):
        calls.append((code, mode, force))
        return None

    monkeypatch.setattr(stocks, "analysis_generate", fake_generate)
    # 清掉可能的残留状态(模块级 registry)
    stocks._single_analysis_jobs.pop("600000", None)

    out = stocks.generate_analysis("600000", mode="single", force=True, db=db, user_id=None)
    assert out.started is True
    assert out.already_running is False

    st = _wait_done("600000")
    assert st.running is False
    assert st.error is None
    assert calls == [("600000", "single", True)], "后台应以原参数恰调一次 analysis_generate"
    print("✓ 异步启动 → 后台成功 → 状态可查")


def test_status_carries_error(monkeypatch):
    db = _db()
    db.add(Watchlist(id=2, code="600001", user_id=None, name="测试", exchange="sh"))
    db.commit()

    def boom(_db, code, *, mode="single", force=False):
        raise RuntimeError("model timed out")

    monkeypatch.setattr(stocks, "analysis_generate", boom)
    stocks._single_analysis_jobs.pop("600001", None)

    out = stocks.generate_analysis("600001", mode="single", force=True, db=db, user_id=None)
    assert out.started is True

    st = _wait_done("600001")
    assert st.running is False
    assert st.error and "timed out" in st.error, "后台异常应透出到 status.error"
    print("✓ 后台失败 → status.error 透出")


def test_not_in_watchlist_404(monkeypatch):
    from fastapi import HTTPException

    db = _db()  # 空自选
    monkeypatch.setattr(stocks, "analysis_generate", lambda *a, **k: None)
    try:
        stocks.generate_analysis("999999", mode="single", force=True, db=db, user_id=None)
    except HTTPException as e:
        assert e.status_code == 404
        print("✓ 不在自选 → 404,不起线程")
        return
    raise AssertionError("expected 404 for code not in watchlist")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
