"""LLM-driven deep analysis pipeline.

Pipeline:
  1. Load latest Snapshot for the code (or pull a fresh one if none exists).
  2. Render a prompt that mixes snapshot data + strategy rules.
  3. Call Claude with a single tool (`submit_analysis`) whose input schema
     enforces the key table shape AND a markdown deep_analysis field.
  4. Persist into the Analysis cache table (one row per code, replaced).

Caching: callers check the table's `created_at` against TTL. We don't TTL
inside this module — the route does it explicitly.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from anthropic import Anthropic
from sqlalchemy.orm import Session
from sqlalchemy import desc

from ..config import settings
from ..models import Analysis, Snapshot, Watchlist
from .strategy import Strategy, get as get_strategy

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"

# Tool schema. Claude is forced to call this; we read the structured input
# back as our analysis. The `additionalProperties: False` constraint and
# enum lists keep the model from drifting out of contract.
ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "提交对该 A 股标的的结构化投资建议。必须调用一次。",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["key_table", "deep_analysis"],
        "properties": {
            "key_table": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "actionable", "buy_price_low", "buy_price_high",
                    "sell_price_low", "sell_price_high", "position_pct",
                    "hold_period", "stop_loss", "confidence", "one_line_reason",
                ],
                "properties": {
                    "actionable": {
                        "type": "string",
                        "enum": ["建议买入", "观望", "建议卖出", "不建议入手"],
                    },
                    "buy_price_low": {"type": "number", "description": "合理买入价区间下限"},
                    "buy_price_high": {"type": "number", "description": "合理买入价区间上限"},
                    "sell_price_low": {"type": "number", "description": "合理卖出价区间下限"},
                    "sell_price_high": {"type": "number", "description": "合理卖出价区间上限"},
                    "position_pct": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "建议仓位百分比 (0-100)",
                    },
                    "hold_period": {
                        "type": "string",
                        "enum": ["短线 1-2周", "中线 1-3月", "长线 6月+"],
                    },
                    "stop_loss": {"type": "number", "description": "止损价"},
                    "confidence": {"type": "string", "enum": ["高", "中", "低"]},
                    "one_line_reason": {
                        "type": "string",
                        "description": "一句话理由（不超过 40 字）",
                    },
                },
            },
            "deep_analysis": {
                "type": "string",
                "description": (
                    "约 500 字 markdown 深度分析，分四节："
                    "## 基本面 / ## 技术面 / ## 消息面 / ## 风险点。"
                    "不要超过 500 字。"
                ),
            },
        },
    },
}


def _system_prompt(strategy: Strategy) -> list[dict[str, Any]]:
    """Returns Anthropic-format system blocks. The static parts use prompt
    caching so re-using the same strategy is cheap."""
    base = (
        "你是一名审慎的 A 股投资分析助手，目标用户是一支 10 人的小型投资团队。\n"
        "请完全基于提供的 snapshot 数据与新闻/公告做判断，不要编造未在输入中出现的信息。\n"
        "如果某个维度信息缺失，明确说明并降低置信度。\n"
        "始终调用 submit_analysis 工具一次，不要给出其他文本。\n"
    )
    rules_section = ""
    if strategy.rules:
        rules_section = (
            "\n\n## 必须遵守的硬规则\n"
            + "\n".join(f"- {r}" for r in strategy.rules)
            + "\n违反任何一条都应直接给出 actionable=不建议入手。"
        )
    return [{
        "type": "text",
        "text": base + rules_section,
        "cache_control": {"type": "ephemeral"},
    }]


def _user_prompt(w: Watchlist, s: Snapshot | None) -> str:
    if s is None:
        snap_section = "（暂无 snapshot 数据，请基于代码、名称、市场常识做最低限度的判断，并显著降低置信度。）"
    else:
        news_lines = "\n".join(
            f"- [{n.get('ts','')}] {n.get('title','')}" for n in (s.news or [])[:5]
        ) or "（无）"
        notice_lines = "\n".join(
            f"- [{n.get('ts','')}] {n.get('title','')}（{n.get('type') or '一般'}）"
            for n in (s.notices or [])[:5]
        ) or "（无）"
        lhb = (
            f"上榜原因={s.lhb.get('reason','')}, 净买额={s.lhb.get('net_buy')}"
            if s.lhb else "（今日未上榜）"
        )
        snap_section = (
            f"快照时间: {s.ts.isoformat()}\n"
            f"最新价: {s.price}\n"
            f"涨跌幅: {s.change_pct}%\n"
            f"成交量: {s.volume}\n"
            f"成交额: {s.turnover} 元\n"
            f"主力净流入: {s.main_net_flow} 元\n"
            f"命中信号: {', '.join(s.signals or []) or '（无）'}\n\n"
            f"## 最近新闻\n{news_lines}\n\n"
            f"## 最近公告\n{notice_lines}\n\n"
            f"## 龙虎榜\n{lhb}\n"
        )

    return (
        f"## 标的\n代码: {w.code}\n名称: {w.name}\n市场: {w.exchange}\n\n"
        f"## 最新 snapshot\n{snap_section}\n\n"
        f"请基于上面的信息调用 submit_analysis 一次。"
    )


def generate(
    db: Session,
    code: str,
    strategy_name: str | None = None,
    client: Anthropic | None = None,
) -> Analysis:
    """Synchronously generate a fresh analysis and persist it.

    Replaces the existing Analysis row for this code (one row per code policy).
    """
    w = db.query(Watchlist).filter(Watchlist.code == code).first()
    if not w:
        raise ValueError(f"{code} not in watchlist")

    s = (
        db.query(Snapshot)
        .filter(Snapshot.code == code)
        .order_by(desc(Snapshot.id))
        .first()
    )

    strat = get_strategy(strategy_name)
    if client is None:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Add it in Railway → backend → Variables."
            )
        kwargs: dict[str, Any] = {"api_key": settings.ANTHROPIC_API_KEY}
        if settings.ANTHROPIC_BASE_URL:
            kwargs["base_url"] = settings.ANTHROPIC_BASE_URL
        client = Anthropic(**kwargs)

    msg = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_system_prompt(strat),
        tools=[ANALYSIS_TOOL],
        tool_choice={"type": "tool", "name": "submit_analysis"},
        messages=[{"role": "user", "content": _user_prompt(w, s)}],
    )

    tool_use = next((b for b in msg.content if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError("Claude did not return a tool_use block")

    payload: dict[str, Any] = tool_use.input  # type: ignore[assignment]
    if "key_table" not in payload or "deep_analysis" not in payload:
        raise RuntimeError(f"unexpected tool input: {json.dumps(payload)[:200]}")

    existing = db.query(Analysis).filter(Analysis.code == code).first()
    if existing:
        existing.key_table = payload["key_table"]
        existing.deep_analysis = payload["deep_analysis"]
        existing.snapshot_id = s.id if s else None
        existing.model = MODEL
        existing.strategy = strat.name
        existing.created_at = datetime.now(timezone.utc)
        row = existing
    else:
        row = Analysis(
            code=code,
            key_table=payload["key_table"],
            deep_analysis=payload["deep_analysis"],
            snapshot_id=s.id if s else None,
            model=MODEL,
            strategy=strat.name,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_cached(db: Session, code: str, max_age_hours: int = 4) -> Analysis | None:
    """Return cached analysis if it's still fresh, else None."""
    row = db.query(Analysis).filter(Analysis.code == code).first()
    if row is None:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    created = row.created_at
    if created.tzinfo is None:  # SQLite returns naive
        created = created.replace(tzinfo=timezone.utc)
    if created < cutoff:
        return None
    return row
