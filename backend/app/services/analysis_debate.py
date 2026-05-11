"""Three-role analysis debate: 看多 → 看空 → 裁判.

For "high-stakes" picks (high suggested position, conviction calls) we
spend 3x the LLM cost to get sharper red-flag detection. The single-pass
LLM tends toward self-reinforcing narratives; an adversarial setup forces
both sides to be made explicit before the judge collapses them into a
final call.

Pipeline:
  1. Bull analyst: list all reasons to buy + targets + key catalysts
  2. Bear analyst: list all reasons to sell + risks + breakdown levels
  3. Judge: ingest both views + the raw snapshot, emit the full
     submit_analysis tool call (same schema as the single-pass flow)
     with `actionable` reflecting the cross-examination

The judge's payload still goes through the standard validators in
analysis.generate(), so ST overrides etc. still apply.

Selectivity (cost control):
- Default analysis path stays single-pass
- Debate triggers automatically when single-pass returns high-conviction
  buy or sell with position_pct >= 30 — those are the calls where wrong
  costs the user real money
- Users can also explicitly request debate via ?mode=debate query param
"""
from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import Anthropic

from ..config import settings
from ..models import Snapshot, Watchlist
from .strategy import Strategy

logger = logging.getLogger(__name__)


# ---- Bull / Bear tool schemas ----
# Smaller than submit_analysis on purpose — these turns are just for
# argument generation, the judge does the final synthesis.

BULL_TOOL = {
    "name": "submit_bull_view",
    "description": "提交对本股的看多论据。必须调用一次。",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["thesis", "key_points", "target_price", "catalysts"],
        "properties": {
            "thesis": {
                "type": "string",
                "minLength": 30,
                "description": "一段话总结看多核心逻辑，≤120 字",
            },
            "key_points": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "items": {"type": "string"},
                "description": "支撑买入的具体证据点，每条 ≤ 40 字。覆盖技术 / 资金 / 业绩 / 题材中至少 2 个维度。",
            },
            "target_price": {
                "type": "number",
                "description": "看多情景的目标价",
            },
            "catalysts": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {"type": "string"},
                "description": "未来 1-2 周可能推涨的具体催化（公告 / 业绩 / 政策 / 资金事件），每条 ≤ 30 字",
            },
        },
    },
}

BEAR_TOOL = {
    "name": "submit_bear_view",
    "description": "提交对本股的看空论据。必须调用一次。",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["thesis", "key_points", "stop_loss_level", "risk_triggers"],
        "properties": {
            "thesis": {
                "type": "string",
                "minLength": 30,
                "description": "一段话总结看空核心逻辑，≤120 字",
            },
            "key_points": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "items": {"type": "string"},
                "description": "支撑卖出/不入手的具体证据点，每条 ≤ 40 字。覆盖技术 / 估值 / 业绩 / 风险 中至少 2 个维度。",
            },
            "stop_loss_level": {
                "type": "number",
                "description": "看空情景的关键破位价",
            },
            "risk_triggers": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {"type": "string"},
                "description": "可能引发下跌的具体风险事件，每条 ≤ 30 字",
            },
        },
    },
}


def _bull_system_prompt() -> str:
    return (
        "你是 A 股看多分析师。任务：基于给定的 snapshot，列举所有支持**买入**"
        "本股的具体证据。\n\n"
        "**纪律**：\n"
        "- 只看正面信号：技术面突破 / 资金流入 / 业绩兑现 / 题材催化\n"
        "- 不要做平衡——风险点交给看空团队\n"
        "- 但**不要编造**：所有论据必须有 snapshot 中的依据\n"
        "- 如果实在找不到 2 个以上有力论据，thesis 里直说"
        "「难以找到充分买入理由，建议放弃」\n"
        "- 一次性调用 submit_bull_view 工具提交\n"
    )


def _bear_system_prompt() -> str:
    return (
        "你是 A 股看空分析师。任务：基于给定的 snapshot，列举所有支持**卖出"
        "/不入手**本股的具体风险点。\n\n"
        "**纪律**：\n"
        "- 只看风险信号：技术破位 / 估值泡沫 / 业绩塌方 / 减持 / 题材证伪\n"
        "- 不要做平衡——亮点交给看多团队\n"
        "- 但**不要编造**：所有论据必须有 snapshot 中的依据\n"
        "- 如果真的找不到风险点，thesis 写「目前未发现重大风险」+ 给出"
        "「假如行情转向时」的关键破位价\n"
        "- 一次性调用 submit_bear_view 工具提交\n"
    )


def _run_role(
    client: Anthropic, model: str, system_prompt: str, tool: dict,
    user_message: str, tool_name: str,
) -> dict[str, Any]:
    """Single LLM turn. Forced tool_choice on the supplied tool; same
    fallback to 'any' as analysis.generate() for providers that don't
    take the strict form."""
    base = {
        "model": model,
        "max_tokens": 4096,
        "system": system_prompt,
        "tools": [tool],
        "messages": [{"role": "user", "content": user_message}],
    }
    try:
        msg = client.messages.create(
            **base,
            tool_choice={"type": "tool", "name": tool_name},
        )
    except Exception as e:
        if "tool_choice" in str(e) or "400" in str(e):
            logger.info("model %s rejected forced tool_choice for %s; retrying with 'any'",
                        model, tool_name)
            msg = client.messages.create(**base, tool_choice={"type": "any"})
        else:
            raise
    tool_use = next((b for b in msg.content if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError(f"model did not return a tool_use for {tool_name}")
    return tool_use.input  # type: ignore[return-value]


def run_debate(
    client: Anthropic, model: str, w: Watchlist, s: Snapshot | None,
    base_user_prompt: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run bull + bear in sequence. Returns (bull_payload, bear_payload).
    Sequential, not parallel — kimi backend doesn't like double-fire from
    one client; sequential is ~30s total which is acceptable."""
    bull = _run_role(
        client, model, _bull_system_prompt(), BULL_TOOL,
        base_user_prompt, "submit_bull_view",
    )
    bear = _run_role(
        client, model, _bear_system_prompt(), BEAR_TOOL,
        base_user_prompt, "submit_bear_view",
    )
    return bull, bear


def render_debate_for_judge(bull: dict[str, Any], bear: dict[str, Any]) -> str:
    """Format the bull/bear outputs into a markdown block the judge sees
    before emitting the final submit_analysis call."""
    def _bullet(items):
        return "\n".join(f"- {x}" for x in (items or []))

    return (
        "\n\n## 看多分析师视角\n"
        f"**核心论点**：{bull.get('thesis','—')}\n\n"
        f"**关键证据**：\n{_bullet(bull.get('key_points'))}\n\n"
        f"**目标价**：{bull.get('target_price','—')}\n\n"
        f"**未来催化**：\n{_bullet(bull.get('catalysts'))}\n\n"
        "## 看空分析师视角\n"
        f"**核心论点**：{bear.get('thesis','—')}\n\n"
        f"**关键证据**：\n{_bullet(bear.get('key_points'))}\n\n"
        f"**关键破位价**：{bear.get('stop_loss_level','—')}\n\n"
        f"**风险触发**：\n{_bullet(bear.get('risk_triggers'))}\n\n"
        "## 你的任务\n"
        "你是裁判。综合 snapshot 原始数据 + 上述看多/看空双方论据，给出最终结构化分析。\n"
        "- 哪方论据更扎实、更接地气，向哪方倾斜\n"
        "- 双方都提到的风险点优先反映到 red_flags\n"
        "- 看多/看空都不充分时给 '观望'\n"
        "- confidence: 双方论据都强 → 高；一方明显占优 → 中；双方都弱 → 低\n"
    )


def judge_system_prompt_suffix() -> str:
    """Extra system-prompt suffix the judge sees on top of the standard
    analysis system prompt. Slot is appended in services.analysis.generate()
    when mode='debate'."""
    return (
        "\n\n# 辩论模式补充\n"
        "本次分析走的是辩论模式：你已经看过看多和看空分析师的视角。"
        "你的工作是**裁判**——不要简单复述任何一方，而是基于双方证据 + "
        "原始 snapshot 做综合判断。如果双方论据都偏弱，confidence 给 '低'；"
        "如果一方明显占优，向其倾斜并解释为什么对方论据不成立。"
    )
