"""单股深挖档(deep mode)adapter 单测 —— mock httpx, 不打网络。

覆盖 analysis_thinking.call_thinking 的核心逻辑:
  1. happy: SSE 分片累积 tool args → payload dict;reasoning_content 只 log
     不入 payload。
  2. retry: 第一次 auto 没调 tool(无 tool_calls)→ 第二次成功 → 返回。
  3. fail: 两次都坏 → RuntimeError(被 route 转 503)。
  4. config: 没配 OPENAI_COMPAT_BASE_URL → RuntimeError。

跑法:cd backend && .venv/bin/python tests/test_deep_mode.py
(也兼容 pytest:pytest tests/test_deep_mode.py)
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# settings 是模块级单例 —— 必须在 import app.config 前把 deep 档 env 设好。
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:5432/db")
os.environ.setdefault("AUTH_SECRET", "x")
os.environ.setdefault("OPENAI_COMPAT_BASE_URL", "https://fake.example/compatible-mode/v1")
os.environ.setdefault("OPENAI_COMPAT_API_KEY", "fake-key")

from app.services import analysis_thinking as at


# --- mock httpx.stream ------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code: int, lines: list[str]):
        self.status_code = status_code
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        return b'{"error":"fake"}'


def _sse(payload_dict: dict, reasoning: str = "", arg_chunks: int = 3) -> list[str]:
    """构造一段 SSE: reasoning delta + tool_calls.arguments 分片 + usage + DONE。"""
    raw = json.dumps(payload_dict, ensure_ascii=False)
    # 把 args 拆成 arg_chunks 片, 模拟流式分片到达
    step = max(1, len(raw) // arg_chunks)
    pieces = [raw[i:i + step] for i in range(0, len(raw), step)]
    lines: list[str] = []
    if reasoning:
        lines.append("data: " + json.dumps(
            {"choices": [{"delta": {"reasoning_content": reasoning}}]}, ensure_ascii=False))
    for p in pieces:
        lines.append("data: " + json.dumps(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": p}}]}}]},
            ensure_ascii=False))
    lines.append("data: " + json.dumps(
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"completion_tokens": 999,
                   "completion_tokens_details": {"reasoning_tokens": 123}}},
        ensure_ascii=False))
    lines.append("data: [DONE]")
    return lines


def _patch_stream(monkey_lines_seq: list[list[str]], statuses: list[int] | None = None):
    """返回一个 fake httpx.stream;按调用次序吐 monkey_lines_seq[i]。"""
    calls = {"n": 0}

    def _stream(method, url, **kw):
        i = calls["n"]
        calls["n"] += 1
        st = (statuses or [200] * len(monkey_lines_seq))[i]
        return _FakeResp(st, monkey_lines_seq[i])

    return _stream, calls


GOOD_PAYLOAD = {
    "analysis_thinking": "先看技术面再看资金面……（CoT scratchpad）",
    "key_table": {"actionable": "建议卖出", "confidence": 80, "red_flags": ["由盈转亏"]},
    "deep_analysis": "## 公司画像\n这是一段深度分析。",
}


def test_happy(monkeypatch):
    stream, calls = _patch_stream([_sse(GOOD_PAYLOAD, reasoning="一大段原生推理内容" * 5)])
    monkeypatch.setattr(at.httpx, "stream", stream)
    payload = at.call_thinking("sys", "user", "qwen3.7-max")
    assert calls["n"] == 1, "happy 应只调一次"
    assert payload["key_table"]["actionable"] == "建议卖出"
    assert "deep_analysis" in payload and "key_table" in payload
    # reasoning 是模型原生推理, 不应进 payload(payload 只是 tool args)
    assert "reasoning_content" not in payload and "reasoning" not in payload
    print("PASS test_happy")


def test_retry_then_ok(monkeypatch):
    # 第一次:auto 没调 tool(delta 只有 content, 无 tool_calls)→ no_tool_call
    bad = ["data: " + json.dumps({"choices": [{"delta": {"content": "我觉得……"}, "finish_reason": "stop"}]}),
           "data: [DONE]"]
    good = _sse(GOOD_PAYLOAD)
    stream, calls = _patch_stream([bad, good])
    monkeypatch.setattr(at.httpx, "stream", stream)
    payload = at.call_thinking("sys", "user", "qwen3.7-max")
    assert calls["n"] == 2, "应 retry 一次"
    assert payload["key_table"]["actionable"] == "建议卖出"
    print("PASS test_retry_then_ok")


def test_two_fail_raises(monkeypatch):
    bad = ["data: " + json.dumps({"choices": [{"delta": {"content": "x"}}]}), "data: [DONE]"]
    stream, calls = _patch_stream([bad, bad])
    monkeypatch.setattr(at.httpx, "stream", stream)
    try:
        at.call_thinking("sys", "user", "qwen3.7-max")
        assert False, "两次坏应 raise RuntimeError"
    except RuntimeError as e:
        assert "深挖档" in str(e)
    assert calls["n"] == 2
    print("PASS test_two_fail_raises")


def test_incomplete_payload_retries(monkeypatch):
    # tool args 解析成功但缺 deep_analysis → incomplete → retry
    partial = {"key_table": {"actionable": "观望"}}  # 缺 deep_analysis
    stream, calls = _patch_stream([_sse(partial), _sse(GOOD_PAYLOAD)])
    monkeypatch.setattr(at.httpx, "stream", stream)
    payload = at.call_thinking("sys", "user", "qwen3.7-max")
    assert calls["n"] == 2
    assert "deep_analysis" in payload
    print("PASS test_incomplete_payload_retries")


def test_missing_base_url_raises(monkeypatch):
    monkeypatch.setattr(at.settings, "OPENAI_COMPAT_BASE_URL", "")
    try:
        at.call_thinking("sys", "user", "qwen3.7-max")
        assert False, "无 base_url 应 raise"
    except RuntimeError as e:
        assert "OPENAI_COMPAT_BASE_URL" in str(e)
    print("PASS test_missing_base_url_raises")


# --- 极简 monkeypatch shim(无 pytest 时也能 python 直跑)---------------------

class _MP:
    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, val):
        old = getattr(obj, name)
        self._undo.append((obj, name, old))
        setattr(obj, name, val)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)
        self._undo.clear()


if __name__ == "__main__":
    for fn in (test_happy, test_retry_then_ok, test_two_fail_raises,
               test_incomplete_payload_retries, test_missing_base_url_raises):
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp.undo()
    print("\nALL DEEP-MODE TESTS PASSED")
