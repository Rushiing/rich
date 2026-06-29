"""单股深挖档 (deep mode) — OpenAI 兼容协议 + thinking 模型 adapter.

为什么独立成一条路径(不复用 analysis.py 的 Anthropic SDK 调用):
  这次评估 Aliyun token-plan MaaS 的一批模型时发现,该 provider 的
  **Anthropic 兼容层对 thinking 模型是坏的** —— force tool_choice 要么 400
  (deepseek/qwen 强制 thinking),要么静默吐损坏 tool JSON(kimi 系列)。
  只有走 **OpenAI 兼容协议(/compatible-mode/v1) + stream + tool_choice=auto**
  才能稳定拿到结构化输出,且 reasoning_content 真生效(实测 qwen3.7-max
  6769 字推理)。

设计要点:
  - 产出与 Anthropic 路径**完全一致的 payload dict**(submit_analysis 的
    input),让 analysis.generate() 后半的 validate / 持久化 / record_anchor
    逻辑 100% 复用,零持久化改动。
  - tool_choice 统一 "auto":deepseek/qwen 强制 thinking 只接受 auto;auto
    偶发不调 tool 由本模块 retry 1 次兜底。
  - reasoning_content 只累积计长度 log,不入 payload(它是模型原生推理,
    不是 schema 内的 analysis_thinking)。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

# 单股、用户主动等 ~90s,给足超时。比 batch 的 180 更宽,因为 thinking 模型
# 的 reasoning + 结构化输出在慢时段可能到 100s+。
_DEEP_TIMEOUT = 240.0

# 必填的两个顶层字段 —— 缺任一即视为输出不完整(auto 偶发只吐 content 不调
# tool,或 stream 中断截断 JSON),触发 retry。
_REQUIRED_KEYS = ("key_table", "deep_analysis")


def _openai_tool() -> dict[str, Any]:
    """把生产的 ANALYSIS_TOOL(Anthropic 格式)转成 OpenAI function 格式。
    复用同一份 schema,不复制,避免漂移。"""
    from .analysis import ANALYSIS_TOOL
    return {
        "type": "function",
        "function": {
            "name": ANALYSIS_TOOL["name"],
            "description": ANALYSIS_TOOL["description"],
            "parameters": ANALYSIS_TOOL["input_schema"],
        },
    }


def _stream_once(base_url: str, api_key: str, model: str,
                 system_text: str, user_content: str) -> tuple[dict[str, Any] | None, int, str]:
    """一次 stream 调用。返回 (payload_or_None, reasoning_len, err_tag)。

    err_tag 为 "" 表示成功;否则是简短诊断("http_400" / "no_tool_call" /
    "json_parse" / "incomplete")供调用方决定是否 retry。
    """
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 8192,
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_content},
        ],
        "tools": [_openai_tool()],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if settings.ANALYSIS_DEEP_ENABLE_THINKING:
        body["enable_thinking"] = True

    reasoning_parts: list[str] = []
    arg_parts: list[str] = []
    usage: dict[str, Any] = {}
    finish: str | None = None

    try:
        with httpx.stream(
            "POST", f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body, timeout=_DEEP_TIMEOUT,
        ) as r:
            if r.status_code != 200:
                txt = r.read().decode("utf-8", "ignore")
                logger.warning("deep adapter HTTP %s: %s", r.status_code, txt[:300])
                return None, 0, f"http_{r.status_code}"
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk_s = line[5:].strip()
                if chunk_s == "[DONE]":
                    break
                try:
                    chunk = json.loads(chunk_s)
                except Exception:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices", []):
                    delta = ch.get("delta", {}) or {}
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        reasoning_parts.append(rc)
                    for tc in delta.get("tool_calls", []) or []:
                        fn = tc.get("function", {}) or {}
                        if fn.get("arguments"):
                            arg_parts.append(fn["arguments"])
                    if ch.get("finish_reason"):
                        finish = ch["finish_reason"]
    except Exception as e:
        logger.warning("deep adapter stream error: %s", str(e)[:200])
        return None, 0, f"stream_err:{type(e).__name__}"

    reasoning_len = sum(len(p) for p in reasoning_parts)
    raw_args = "".join(arg_parts)
    if not raw_args:
        # auto 没调 tool(只吐了 content),或 gateway 没透传 tool_calls。
        logger.warning("deep adapter no tool_call (finish=%s, reasoning=%d字)", finish, reasoning_len)
        return None, reasoning_len, "no_tool_call"
    try:
        payload = json.loads(raw_args)
    except Exception as e:
        logger.warning("deep adapter tool args json parse failed (%s): %r", e, raw_args[:200])
        return None, reasoning_len, "json_parse"
    if not isinstance(payload, dict) or any(k not in payload for k in _REQUIRED_KEYS):
        logger.warning(
            "deep adapter incomplete payload: keys=%s",
            list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        )
        return None, reasoning_len, "incomplete"

    rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") \
        if isinstance(usage.get("completion_tokens_details"), dict) else None
    logger.info(
        "deep adapter ok: model=%s reasoning=%d字/%stok out_tok=%s finish=%s",
        model, reasoning_len, rt, usage.get("completion_tokens"), finish,
    )
    return payload, reasoning_len, ""


def call_thinking(system_text: str, user_content: str, model: str) -> dict[str, Any]:
    """单股深挖:调 OpenAI 兼容 thinking 模型,返回 submit_analysis 的 input dict。

    与 Anthropic 路径输出同形状(含 analysis_thinking / key_table /
    deep_analysis),交给 analysis.generate() 后半统一持久化。

    失败模式(无 tool_call / json 截断 / payload 不完整)retry 1 次;仍失败
    raise RuntimeError(被 route 转成 503)。
    """
    base_url = settings.OPENAI_COMPAT_BASE_URL.rstrip("/")
    api_key = settings.OPENAI_COMPAT_API_KEY or settings.ANTHROPIC_API_KEY
    if not base_url:
        raise RuntimeError("深挖档未配置 OPENAI_COMPAT_BASE_URL")
    if not api_key:
        raise RuntimeError("深挖档未配置 API key(OPENAI_COMPAT_API_KEY / ANTHROPIC_API_KEY)")

    payload, _, err = _stream_once(base_url, api_key, model, system_text, user_content)
    if err:
        logger.info("deep adapter[%s] %s on first try, retrying once...", model, err)
        payload, _, err = _stream_once(base_url, api_key, model, system_text, user_content)
        if err:
            raise RuntimeError(f"深挖档 LLM 输出不可用(err={err}, model={model})")
    assert payload is not None  # err == "" 保证
    return payload
