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

# Default analysis model. Overridable via ANALYSIS_MODEL env var without a
# code change — useful when the upstream gateway shifts models or when we
# want to A/B between providers (e.g. Sonnet vs kimi-k2.5).
#
# Why kimi-k2.5 (4/28): the Sonnet quota was exhausted; we benchmarked the
# 5 viable dashscope models on 300638 (a hard case) and kimi was the best
# trade-off — fastest reliable (~25s), most concise output, tied for most
# thorough red_flags, and the only fast one that supports the *forced*
# `tool_choice={"type":"tool", "name": ...}` shape so analysis.py needs
# no protocol change. Tone matches the user's "克制研究员" preference.
DEFAULT_MODEL = "kimi-k2.5"

# Tool schema. Claude is forced to call this; we read the structured input
# back as our analysis. The `additionalProperties: False` constraint + enums
# keep the model from drifting out of contract.
#
# v2 (4/27): expanded to match the editorial template the user shared
# (益佰制药 example): structured red-flag detection, multi-tier stop-loss
# levels, scenario-based action advice, 8-dimension risk scorecard.
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
                    "company_tag",
                    "actionable", "one_line_reason",
                    "red_flags",
                    "buy_price_low", "buy_price_high",
                    "sell_price_low", "sell_price_high",
                    "position_pct", "hold_period",
                    "stop_loss_levels",
                    "scenario_advice",
                    "risk_scores",
                    "confidence",
                ],
                "properties": {
                    "company_tag": {
                        "type": "string",
                        "description": (
                            '一句话公司画像，30 字内。格式参考："贵州中药老字号 + '
                            '业绩塌方 + 维权标记" 这种用 "+" 串起来的特征拼接。'
                        ),
                    },
                    "actionable": {
                        "type": "string",
                        "enum": ["建议买入", "观望", "建议卖出", "不建议入手"],
                    },
                    "one_line_reason": {
                        "type": "string",
                        "description": "一句话理由，不超过 40 字。直说，不要和稀泥。",
                    },
                    "red_flags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "硬标识检测，每条 ≤ 25 字。常见红旗：ST 标记 / 维权标记 / "
                            "曾被监管处罚 / 异常波动公告但公司声明无信息 / 由盈转亏 / "
                            "营收同比转负 / 三费占营收比 > 50% / 控股股东减持 / "
                            "限售解禁压力。没有就空数组。"
                        ),
                    },
                    "buy_price_low": {"type": "number", "description": "合理买入价区间下限"},
                    "buy_price_high": {"type": "number", "description": "合理买入价区间上限"},
                    "sell_price_low": {"type": "number", "description": "合理卖出价区间下限"},
                    "sell_price_high": {"type": "number", "description": "合理卖出价区间上限"},
                    "position_pct": {
                        "type": "number", "minimum": 0, "maximum": 100,
                        "description": "建议仓位百分比 (0-100)",
                    },
                    "hold_period": {
                        "type": "string",
                        "enum": ["短线 1-2周", "中线 1-3月", "长线 6月+"],
                    },
                    "stop_loss_levels": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "description": (
                            "止损线分档。最少 1 档（紧急），最多 3 档（紧急/中线/深跌）。"
                            "高危票应给齐 3 档，普通票 1-2 档即可。"
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["price", "label", "reason"],
                            "properties": {
                                "price": {"type": "number"},
                                "label": {
                                    "type": "string",
                                    "enum": ["紧急止损", "中线止损", "深跌止损"],
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "为什么是这个价 + 跌破后该做什么。≤ 50 字。",
                                },
                            },
                        },
                    },
                    "scenario_advice": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "not_holding",
                            "holding_big_gain",
                            "holding_small",
                            "holding_big_loss",
                        ],
                        "description": "按持仓情境给的具体动作建议，每条 ≤ 40 字。",
                        "properties": {
                            "not_holding": {
                                "type": "string",
                                "description": "未持仓的人怎么做。",
                            },
                            "holding_big_gain": {
                                "type": "string",
                                "description": "已持仓且大幅浮盈怎么做。",
                            },
                            "holding_small": {
                                "type": "string",
                                "description": "已持仓且小幅浮盈/浮亏怎么做。",
                            },
                            "holding_big_loss": {
                                "type": "string",
                                "description": "已持仓且大幅浮亏怎么做。",
                            },
                        },
                    },
                    "risk_scores": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "fundamentals", "valuation", "earnings_momentum",
                            "industry", "governance", "price_action",
                            "capital", "thematic", "overall",
                        ],
                        "description": (
                            "8 个维度评分（1-5 ⭐），加一个综合等级。"
                            "评分时不要平均化，差就是差，好就是好。"
                        ),
                        "properties": {
                            "fundamentals":     {"type": "integer", "minimum": 1, "maximum": 5, "description": "基本面"},
                            "valuation":        {"type": "integer", "minimum": 1, "maximum": 5, "description": "估值"},
                            "earnings_momentum":{"type": "integer", "minimum": 1, "maximum": 5, "description": "业绩兑现节奏"},
                            "industry":         {"type": "integer", "minimum": 1, "maximum": 5, "description": "行业景气度"},
                            "governance":       {"type": "integer", "minimum": 1, "maximum": 5, "description": "公司治理"},
                            "price_action":     {"type": "integer", "minimum": 1, "maximum": 5, "description": "股价表现"},
                            "capital":           {"type": "integer", "minimum": 1, "maximum": 5, "description": "资金面"},
                            "thematic":         {"type": "integer", "minimum": 1, "maximum": 5, "description": "题材炒作"},
                            "overall": {
                                "type": "string",
                                "enum": [
                                    "⭐ 极差", "⭐⭐ 较差", "⭐⭐⭐ 一般",
                                    "⭐⭐⭐⭐ 较好", "⭐⭐⭐⭐⭐ 极好",
                                ],
                                "description": "综合评级",
                            },
                        },
                    },
                    "confidence": {"type": "string", "enum": ["高", "中", "低"]},
                },
            },
            "deep_analysis": {
                "type": "string",
                "description": (
                    "1500-2500 字 markdown 深度分析，按以下章节展开（每节用 ## 标题）：\n"
                    "## 公司画像\n"
                    "## 最新业绩\n"
                    "## 股价剧情\n"
                    "## 看多 vs 看空\n"
                    "## 风险与止损\n"
                    "## 操作建议\n"
                    "## 一句话总结\n\n"
                    "**风格要求（必须遵守）**：\n"
                    "1. 大白话，第一人称对您说，像跟朋友聊天\n"
                    "2. 直接表态：'清仓' / '不要补仓' / '别参与'，不写'谨慎' / '建议' / '可关注' 这类和稀泥的词\n"
                    "3. 关键数字 **加粗**，关键风险用 ⚠️，硬红灯用 🔴\n"
                    "4. 用有序列表 (1./2./3.) 拆步骤，无序列表 (-) 列要点\n"
                    "5. 不堆术语：把 PE / MACD / 主力净流入 / 北向资金 这种翻译成人话\n"
                    "6. 信息缺失就直说'信息不全，无法判断'，不要硬编"
                ),
            },
        },
    },
}


def _system_prompt(strategy: Strategy) -> list[dict[str, Any]]:
    """Returns Anthropic-format system blocks. The static parts use prompt
    caching so re-using the same strategy is cheap."""
    base = (
        "你是一位经验老到的 A 股投资顾问，目标用户是一支 10 人左右的小型投资团队。\n"
        "你的任务是基于 snapshot 给出**结构化、可执行**的投资建议。\n"
        "\n"
        "# 风格（很重要，请严格遵守）\n"
        "\n"
        "1. **大白话**。把 'PE / MACD / 主力净流入 / 北向资金 / 三费' 这种术语翻译成人话再说。\n"
        "2. **第一人称对话**。'我看下来这只票...'、'您如果已经持仓...'、'我建议您...'，像跟朋友聊天。\n"
        "3. **直接表态，不和稀泥**。说 '清仓' / '不要补仓' / '别参与'，不要说 '谨慎' / '可关注' / '建议结合自身情况'。\n"
        "4. **关键数字加粗**（**+8.13%**、**-2.7亿**），关键风险用 ⚠️，硬红灯用 🔴。\n"
        "5. **有结构有层次**。用有序列表（1./2./3.）拆步骤，无序列表（-）列要点。\n"
        "\n"
        "# 必须做的红旗检查清单\n"
        "\n"
        "发起分析前，按这个清单逐条核对，命中的填进 red_flags（每条 ≤ 25 字）：\n"
        "- 🔴 名称带 ST / 维权（'（维权）' 标记）\n"
        "- 🔴 曾被监管处罚（信息中含 '立案' / '通知书' / '暂停' / '处罚' 等）\n"
        "- 🔴 业绩由盈转亏 / 营收同比转负\n"
        "- 🔴 异常波动公告但公司声明 '无重大信息'\n"
        "- 🔴 三费占营收比 > 50%\n"
        "- 🔴 控股股东减持 / 大额限售解禁\n"
        "没命中就空数组，不要凑数。\n"
        "\n"
        "# 数据原则\n"
        "\n"
        "- 完全基于提供的 snapshot 与新闻 / 公告做判断，**不要编造**未在输入中的信息\n"
        "- 信息缺失就直说 '信息不全，无法判断'，并把 confidence 降到 '低'\n"
        "- 始终调用 submit_analysis 工具**一次**，不要给出其他文本\n"
    )
    rules_section = ""
    if strategy.rules:
        rules_section = (
            "\n\n# 必须遵守的硬规则（来自策略）\n"
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
        # Estimate units to keep the LLM grounded — bare numbers like
        # "1757186000000" are easy to misread; "1.76 万亿" is hard to misread.
        def _yi(v: float | None) -> str:
            if v is None:
                return "未知"
            return f"{v / 1e8:.1f} 亿"

        valuation_section = (
            f"市盈率(PE,动态): {s.pe_ratio if s.pe_ratio is not None else '未知'}"
            + (f"  (行业均值 {s.industry_pe_avg:.1f}, 本股分位 {s.industry_pe_pctile:.0f}%)"
               if s.industry_pe_avg is not None and s.industry_pe_pctile is not None else "")
            + "\n"
            + f"市净率(PB): {s.pb_ratio if s.pb_ratio is not None else '未知'}"
            + (f"  (行业均值 {s.industry_pb_avg:.2f})"
               if s.industry_pb_avg is not None else "")
            + "\n"
            + f"换手率: {f'{s.turnover_rate:.2f}%' if s.turnover_rate is not None else '未知'}\n"
            + f"总市值: {_yi(s.market_cap)}\n"
            + f"流通市值: {_yi(s.circ_market_cap)}\n"
        )
        # Phase 7: 3-day rolling metrics, used by the LLM to spot trends
        # the daily snapshot misses. Rendered only when at least one field
        # is present so the prompt doesn't grow noise on cold-start codes.
        three_day_section = ""
        if (s.change_pct_3d is not None or s.turnover_rate_3d is not None or
                s.net_flow_3d is not None):
            three_day_section = (
                f"\n## 近3日表现\n"
                + f"3日涨幅: {f'{s.change_pct_3d:+.2f}%' if s.change_pct_3d is not None else '未知'}"
                + (f"  (行业分位 {s.industry_change_3d_pctile:.0f}%)"
                   if s.industry_change_3d_pctile is not None else "")
                + "\n"
                + f"3日累计换手率: {f'{s.turnover_rate_3d:.2f}%' if s.turnover_rate_3d is not None else '未知'}\n"
                + f"3日主力净流入: {_yi(s.net_flow_3d) if s.net_flow_3d is not None else '未知'}"
                + (f"  (行业分位 {s.industry_flow_3d_pctile:.0f}%)"
                   if s.industry_flow_3d_pctile is not None else "")
                + "\n"
            )
        industry_line = (
            f"所属行业: {s.industry_name}\n" if s.industry_name else ""
        )
        snap_section = (
            f"快照时间: {s.ts.isoformat()}\n"
            f"{industry_line}"
            f"最新价: {s.price}\n"
            f"涨跌幅: {s.change_pct}%\n"
            f"成交量: {s.volume}\n"
            f"成交额: {s.turnover} 元\n"
            f"主力净流入(当日): {s.main_net_flow} 元\n"
            f"命中信号: {', '.join(s.signals or []) or '（无）'}\n\n"
            f"## 估值与活跃度\n{valuation_section}"
            f"{three_day_section}\n"
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

    model = settings.ANALYSIS_MODEL or DEFAULT_MODEL

    # tool_choice negotiation: most providers accept the strict
    # `{"type":"tool", "name":...}` shape, but some only support `any`/`auto`.
    # We try strict first, fall back to `any` on a 400 — for our single-tool
    # setup the two are functionally equivalent.
    base_kwargs = {
        "model": model,
        "max_tokens": 8192,  # bumped from 4096 — kimi/glm/minimax outputs run
                              # 1k–2k tokens; with thinking-style reasoners
                              # (qwen3.6) we'd hit ceiling at 4096 mid-tool.
        "system": _system_prompt(strat),
        "tools": [ANALYSIS_TOOL],
        "messages": [{"role": "user", "content": _user_prompt(w, s)}],
    }
    try:
        msg = client.messages.create(
            **base_kwargs,
            tool_choice={"type": "tool", "name": "submit_analysis"},
        )
    except Exception as e:
        # Cheap signature check — most "tool_choice not supported" errors come
        # back as a 400 with a message mentioning tool_choice.
        if "tool_choice" in str(e) or "400" in str(e):
            logger.info("model %s rejected forced tool_choice; retrying with 'any'", model)
            msg = client.messages.create(**base_kwargs, tool_choice={"type": "any"})
        else:
            raise

    tool_use = next((b for b in msg.content if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError("model did not return a tool_use block")

    payload: dict[str, Any] = tool_use.input  # type: ignore[assignment]
    if "key_table" not in payload or "deep_analysis" not in payload:
        raise RuntimeError(f"unexpected tool input: {json.dumps(payload)[:200]}")

    existing = db.query(Analysis).filter(Analysis.code == code).first()
    if existing:
        existing.key_table = payload["key_table"]
        existing.deep_analysis = payload["deep_analysis"]
        existing.snapshot_id = s.id if s else None
        existing.model = model
        existing.strategy = strat.name
        existing.created_at = datetime.now(timezone.utc)
        row = existing
    else:
        row = Analysis(
            code=code,
            key_table=payload["key_table"],
            deep_analysis=payload["deep_analysis"],
            snapshot_id=s.id if s else None,
            model=model,
            strategy=strat.name,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def get_cached(db: Session, code: str, max_age_hours: int = 4) -> Analysis | None:
    """Return cached analysis if it's still fresh AND on the v2 schema."""
    row = db.query(Analysis).filter(Analysis.code == code).first()
    if row is None:
        return None
    # Schema v2 invalidation: rows missing the new `company_tag` field were
    # generated against the old key_table schema and need to be regenerated.
    if not isinstance(row.key_table, dict) or "company_tag" not in row.key_table:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    created = row.created_at
    if created.tzinfo is None:  # SQLite returns naive
        created = created.replace(tzinfo=timezone.utc)
    if created < cutoff:
        return None
    return row
