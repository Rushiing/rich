#!/usr/bin/env python3
"""One-off LLM comparison: kimi-k2.5 vs deepseek-v4-flash vs deepseek-v4-pro.

Runs the same analysis pipeline (system prompt + tool schema + mock
snapshot) against three providers in sequence and writes a side-by-side
markdown report. NOT wired to production — does not touch the DB and is
safe to run multiple times.

Usage (from repo root):

  cd backend && python -m tools.compare_models

Required env vars:
  ANTHROPIC_API_KEY     — for kimi via dashscope (existing prod key)
  ANTHROPIC_BASE_URL    — dashscope endpoint (existing prod value)
  DEEPSEEK_API_KEY      — DeepSeek v4 key

Optional:
  COMPARE_CODE          — 6-digit code to use as the mock subject (default
                          600519 贵州茅台). Doesn't actually pull live data;
                          purely a label + identity hint in the prompt.

Output: /tmp/compare_<timestamp>.md plus per-model JSON dumps at
  /tmp/compare_<timestamp>_<model>.json.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow `python -m tools.compare_models` from the backend/ dir
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anthropic import Anthropic  # noqa: E402

from app.services.analysis import ANALYSIS_TOOL, _system_prompt  # noqa: E402
from app.services.strategy import get as get_strategy  # noqa: E402
# NOTE: we deliberately do NOT import _user_prompt — it reaches into the
# DB (kline + financials services) and we want this script to run with
# zero DB access. We hand-build an equivalent user prompt below using
# mock data so each provider sees the same input.


# --- Mock subject -----------------------------------------------------------
# Plausible mid-cap snapshot, designed to give the models something to chew
# on across both bullish (营收持平 / PE 22 ~ 中性偏低) and bearish (主力流出
# / 短期跌 2.5%) angles. Replace freely if you want to test a specific票.

CODE = os.environ.get("COMPARE_CODE", "600519")
NAME = "贵州茅台" if CODE == "600519" else f"测试票-{CODE}"
EXCHANGE = "sh" if CODE.startswith(("60", "68")) else "sz" if CODE.startswith(("00", "30")) else "bj"


def _mock_user_prompt() -> str:
    """Hand-built equivalent of services.analysis._user_prompt for a mock
    snapshot. Includes the same section structure the real prompt produces
    (估值 / 三日 / 技术 / 财务 / 新闻) so models that special-case those
    blocks behave normally."""
    return (
        f"## 标的\n"
        f"代码: {CODE}\n"
        f"名称: {NAME}\n"
        f"市场: {EXCHANGE}\n"
        f"\n"
        f"## 最新 snapshot\n"
        f"快照时间: {datetime.now(timezone.utc).isoformat()}\n"
        f"所属行业: 白酒\n"
        f"最新价: 1500.0\n"
        f"涨跌幅: -2.5%\n"
        f"成交量: 2500000\n"
        f"成交额: 3750000000 元\n"
        f"主力净流入(当日): -300000000 元\n"
        f"命中信号: big_outflow\n"
        f"\n"
        f"## 技术面 (2026-05-28)\n"
        f"收盘: 1500.00\n"
        f"MA5/10/20/60: 1525.30 / 1542.10 / 1568.50 / 1612.40\n"
        f"MACD: DIF=-8.20  DEA=-5.40  HIST=-2.80  (DIF 下 DEA)\n"
        f"BOLL: 中=1568.50 上=1620.00 下=1517.00\n"
        f"KDJ: K=22.0 D=28.0 J=10.0\n"
        f"RSI6 / RSI12: 32.0 / 38.5\n"
        f"\n"
        f"近 20 日 K 线（升序）：\n"
        f"日期         开    收    高    低    量\n"
        f"2026-04-30  1680  1675  1685  1668  18万\n"
        f"... (中间略, 整体趋势下行，从 1680 跌至当前 1500，跌幅约 11%) ...\n"
        f"2026-05-26  1545  1538  1551  1535  16万\n"
        f"2026-05-27  1538  1525  1542  1518  19万\n"
        f"2026-05-28  1525  1500  1528  1495  25万\n"
        f"近 20 日最高 1685.00（19 日前），最低 1495.00（0 日前）\n"
        f"\n"
        f"## 估值与活跃度\n"
        f"市盈率(PE,动态): 22.0  (行业均值 28.5, 本股分位 25%)\n"
        f"市净率(PB): 8.5  (行业均值 5.8)\n"
        f"换手率: 0.50%\n"
        f"总市值: 19000.0 亿\n"
        f"流通市值: 19000.0 亿\n"
        f"\n"
        f"## 近3日表现\n"
        f"3日涨幅: -6.20%  (行业分位 12%)\n"
        f"3日累计换手率: 1.45%\n"
        f"3日主力净流入: -8.5 亿  (行业分位 8%)\n"
        f"\n"
        f"## 财务面\n"
        f"最新 (2026-Q1): 营收 280.5亿 (同比 -5.20%)  净利润 145.8亿 (同比 -8.10%)  "
        f"毛利率 +91.20%  净利率 +52.00%  ROE +8.50%  期间费用率 +9.50%\n"
        f"上期对照 (2025-Q4): 营收 425.6亿 (同比 +2.10%)  净利润 198.4亿 (同比 +1.50%)  "
        f"毛利率 +91.50%  净利率 +46.60%  ROE +10.20%  期间费用率 +9.20%\n"
        f"\n"
        f"## 最近新闻\n"
        f"- [2026-05-28] 贵州茅台2026 Q1 营收同比微降 5%, 毛利率持平于 91%\n"
        f"- [2026-05-27] 白酒行业一季度库存周转放缓, 龙头公司价格倒挂压力上升\n"
        f"\n"
        f"## 最近公告\n"
        f"（无）\n"
        f"\n"
        f"## 龙虎榜\n"
        f"（今日未上榜）\n"
        f"\n"
        f"请基于上面的信息调用 submit_analysis 一次。"
    )


# --- Provider matrix --------------------------------------------------------

# Provider transport label: which SDK + endpoint shape we'll use.
#   "anthropic": Anthropic SDK + /anthropic endpoint, tools in Anthropic
#                shape (the path our production analysis.py uses today).
#   "openai-strict": OpenAI SDK + /beta endpoint + strict=true per function.
#                The DeepSeek-recommended path; server-side enforces JSON
#                Schema so nested objects can't be silently stringified.
Provider = tuple[str, str, str, str, str]  # (label, transport, base_url, api_key, model)


def _models() -> list[Provider]:
    out: list[Provider] = []

    kimi_base = os.environ.get("ANTHROPIC_BASE_URL", "")
    kimi_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if kimi_base and kimi_key:
        out.append(("kimi-k2.5 (current prod)", "anthropic", kimi_base, kimi_key, "kimi-k2.5"))
    else:
        print("WARN: ANTHROPIC_BASE_URL / ANTHROPIC_API_KEY not set — skipping kimi")

    ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if ds_key:
        # The /anthropic shape — same one our prod analysis.py uses today,
        # included so we can compare "what would land if we just changed
        # the base_url" against the DeepSeek-recommended /beta path.
        out.append(("deepseek-v4-flash (anthropic ep)", "anthropic",
                    "https://api.deepseek.com/anthropic", ds_key, "deepseek-v4-flash"))
        out.append(("deepseek-v4-pro (anthropic ep)", "anthropic",
                    "https://api.deepseek.com/anthropic", ds_key, "deepseek-v4-pro"))
        # The DeepSeek-recommended strict path. Server validates schema.
        out.append(("deepseek-v4-flash (openai strict)", "openai-strict",
                    "https://api.deepseek.com/beta", ds_key, "deepseek-v4-flash"))
        out.append(("deepseek-v4-pro (openai strict)", "openai-strict",
                    "https://api.deepseek.com/beta", ds_key, "deepseek-v4-pro"))
    else:
        print("WARN: DEEPSEEK_API_KEY not set — skipping deepseek")

    return out


def _anthropic_system_to_text(system_blocks) -> str:
    """Flatten the Anthropic-shape system blocks (list of {type:text,text:..})
    into the single string the OpenAI chat API expects as the system message."""
    if isinstance(system_blocks, str):
        return system_blocks
    parts = []
    for b in system_blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            parts.append(b.get("text", ""))
    return "\n\n".join(parts)


def _to_openai_tool(anth_tool: dict, strict: bool = True) -> dict:
    """Convert our Anthropic-shape ANALYSIS_TOOL into OpenAI function form.
    Strict mode requires additionalProperties:False + all-required at every
    nesting level; our schema is already compliant (verified separately)."""
    return {
        "type": "function",
        "function": {
            "name": anth_tool["name"],
            "description": anth_tool.get("description", ""),
            "parameters": anth_tool["input_schema"],
            "strict": strict,
        },
    }


def _call_anthropic(base_url: str, api_key: str, model: str,
                    system_blocks, user_prompt: str) -> dict:
    """Original path: Anthropic SDK + Anthropic-shape tools."""
    client = Anthropic(api_key=api_key, base_url=base_url)
    base_kwargs = {
        "model": model,
        "max_tokens": 8192,
        "system": system_blocks,
        "tools": [ANALYSIS_TOOL],
        "messages": [{"role": "user", "content": user_prompt}],
    }
    try:
        msg = client.messages.create(
            **base_kwargs,
            tool_choice={"type": "tool", "name": "submit_analysis"},
        )
    except Exception as e:
        if "tool_choice" in str(e) or "400" in str(e):
            msg = client.messages.create(**base_kwargs, tool_choice={"type": "any"})
        else:
            raise

    tool_use = next((b for b in msg.content if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError(
            f"no tool_use block in response. content types: "
            f"{[getattr(b, 'type', '?') for b in msg.content]}"
        )
    return dict(tool_use.input)


def _call_openai_strict(base_url: str, api_key: str, model: str,
                        system_blocks, user_prompt: str) -> dict:
    """DeepSeek-recommended path: OpenAI SDK + /beta endpoint + strict=true.
    Server validates JSON Schema, so nested objects can't be silently
    serialized as strings the way they were on the /anthropic endpoint."""
    from openai import OpenAI  # late import — only needed for this path

    client = OpenAI(api_key=api_key, base_url=base_url)
    system_text = _anthropic_system_to_text(system_blocks)
    tools = [_to_openai_tool(ANALYSIS_TOOL, strict=True)]

    # DeepSeek v4's thinking mode (default-on for v4-flash/v4-pro) rejects
    # *every* forced tool_choice form (specific-name → 400; "required" →
    # 400). Only "auto" works. We rely on the system prompt + the lone
    # registered tool to push the model toward calling it.
    resp = client.chat.completions.create(
        model=model,
        max_tokens=8192,
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_prompt},
        ],
        tools=tools,
        tool_choice="auto",
    )
    choice = resp.choices[0]
    tool_calls = (choice.message.tool_calls or [])
    if not tool_calls:
        raise RuntimeError(
            f"no tool_call in response. message: {choice.message.content!r}"
        )
    args_str = tool_calls[0].function.arguments
    return json.loads(args_str)


def _call_one(label: str, transport: str, base_url: str, api_key: str, model: str,
              system_blocks, user_prompt: str) -> dict:
    if transport == "anthropic":
        return _call_anthropic(base_url, api_key, model, system_blocks, user_prompt)
    elif transport == "openai-strict":
        return _call_openai_strict(base_url, api_key, model, system_blocks, user_prompt)
    else:
        raise ValueError(f"unknown transport: {transport}")


# --- Report rendering -------------------------------------------------------

def _truncate(s: str, n: int = 60) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _normalize_payload(p: dict) -> tuple[dict, str | None]:
    """Some providers (deepseek-v4-flash observed) JSON-encode the nested
    key_table as a string instead of returning it as an object. Decode it
    back to a dict so the renderer doesn't crash. Returns (normalized,
    note-or-None) where note flags schema-compliance issues."""
    notes: list[str] = []
    out = dict(p)
    kt = out.get("key_table")
    if isinstance(kt, str):
        notes.append("⚠️ key_table 以 JSON 字符串返回(schema 非严格)")
        try:
            out["key_table"] = json.loads(kt)
        except Exception:
            notes.append("⚠️ key_table 字符串无法解析为 JSON")
            out["key_table"] = {}
    elif not isinstance(kt, dict):
        notes.append(f"⚠️ key_table 类型 {type(kt).__name__},非 dict/str")
        out["key_table"] = {}
    return out, "; ".join(notes) if notes else None


def _render_report(results: list[dict]) -> str:
    """results items: {label, ok, latency_s, payload | error}"""
    # Normalize each result's payload so renderer never sees a stringified
    # key_table. Carry per-result schema notes into the latency table.
    for r in results:
        if r.get("ok") and r.get("payload"):
            normalized, note = _normalize_payload(r["payload"])
            r["payload"] = normalized
            r["schema_note"] = note

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    lines.append(f"# 模型对比: {NAME} ({CODE}) — {ts}")
    lines.append("")
    lines.append("Mock snapshot: 当前价 1500.0, 当日 -2.5%, 主力净流出 3亿, PE 22, news 2 条 (Q1 营收微降 / 行业库存压力)")
    lines.append("")

    # Latency table
    lines.append("## 用时 / 状态 / Schema 合规")
    lines.append("")
    lines.append("| 模型 | 用时 | 状态 | Schema 备注 |")
    lines.append("|---|---|---|---|")
    for r in results:
        status = "✅ OK" if r["ok"] else f"❌ {r.get('error', '?')[:80]}"
        note = r.get("schema_note") or "—"
        lines.append(f"| {r['label']} | {r['latency_s']:.1f}s | {status} | {note} |")
    lines.append("")

    # key_table side-by-side
    lines.append("## key_table 对比")
    lines.append("")
    header = ["字段"] + [r["label"] for r in results]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))

    fields = [
        ("actionable", "结论"),
        ("one_line_reason", "一句话"),
        ("company_tag", "公司画像"),
        ("position_pct", "建议仓位"),
        ("buy_price_low", "买入下限"),
        ("buy_price_high", "买入上限"),
        ("sell_price_low", "卖出下限"),
        ("sell_price_high", "卖出上限"),
        ("hold_period", "持有期"),
        ("confidence", "置信度"),
    ]
    for key, label in fields:
        row = [label]
        for r in results:
            kt = (r.get("payload") or {}).get("key_table") or {}
            v = kt.get(key)
            row.append(_truncate(str(v) if v is not None else "—", 50))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # red_flags
    lines.append("## red_flags (红旗清单)")
    lines.append("")
    for r in results:
        kt = (r.get("payload") or {}).get("key_table") or {}
        rf = kt.get("red_flags") or []
        lines.append(f"### {r['label']}")
        if rf:
            for f in rf:
                lines.append(f"- {f}")
        else:
            lines.append("- (空)")
        lines.append("")

    # stop_loss_levels
    lines.append("## stop_loss_levels (多档止损)")
    lines.append("")
    for r in results:
        kt = (r.get("payload") or {}).get("key_table") or {}
        levels = kt.get("stop_loss_levels") or []
        lines.append(f"### {r['label']}")
        if levels:
            for lv in levels:
                lines.append(f"- **{lv.get('label')}** @ {lv.get('price')}: {lv.get('reason')}")
        else:
            lines.append("- (空)")
        lines.append("")

    # scenario_advice
    lines.append("## scenario_advice (情境建议)")
    lines.append("")
    for r in results:
        kt = (r.get("payload") or {}).get("key_table") or {}
        sa = kt.get("scenario_advice") or {}
        lines.append(f"### {r['label']}")
        if sa:
            for k, v in sa.items():
                lines.append(f"- **{k}**: {v}")
        else:
            lines.append("- (空)")
        lines.append("")

    # risk_scores
    lines.append("## risk_scores (评分)")
    lines.append("")
    if results:
        labels = [r["label"] for r in results]
        lines.append("| 维度 | " + " | ".join(labels) + " |")
        lines.append("|" + "---|" * (len(labels) + 1))
        dim_keys = [
            ("fundamentals", "基本面"),
            ("valuation", "估值"),
            ("earnings_momentum", "业绩"),
            ("industry", "行业"),
            ("governance", "治理"),
            ("price_action", "股价"),
            ("capital", "资金"),
            ("thematic", "题材"),
            ("overall", "综合"),
        ]
        for key, label in dim_keys:
            row = [label]
            for r in results:
                kt = (r.get("payload") or {}).get("key_table") or {}
                scores = kt.get("risk_scores") or {}
                row.append(_truncate(str(scores.get(key, "—")), 30))
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")

    # deep_analysis full
    lines.append("## deep_analysis (完整正文)")
    lines.append("")
    for r in results:
        deep = (r.get("payload") or {}).get("deep_analysis") or ""
        lines.append(f"### {r['label']}")
        lines.append("")
        if deep:
            lines.append(deep)
        else:
            lines.append("(空)")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# --- Main -------------------------------------------------------------------

def _from_dumps(ts_label: str) -> int:
    """Re-render the report from existing /tmp/compare_<ts>_<model>.json
    dumps without re-calling any LLM. Useful when you tweak the renderer
    and don't want to burn API credits regenerating identical content."""
    results: list[dict] = []
    import glob
    pattern = f"/tmp/compare_{ts_label}_*.json"
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"No dumps matching {pattern}")
        return 1
    for path in files:
        model = path.rsplit("_", 1)[-1].replace(".json", "")
        with open(path) as f:
            payload = json.load(f)
        results.append({
            "label": model,
            "model": model,
            "ok": True,
            "latency_s": 0.0,
            "payload": payload,
        })
    report = _render_report(results)
    report_path = f"/tmp/compare_{ts_label}.md"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Report (from dumps): {report_path}")
    return 0


def main() -> int:
    # `python -m tools.compare_models --from-dumps 20260528_141728`
    # rebuilds the report from existing JSON dumps without calling LLMs.
    if len(sys.argv) >= 3 and sys.argv[1] == "--from-dumps":
        return _from_dumps(sys.argv[2])

    matrix = _models()
    if not matrix:
        print("No models to test — set ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL or DEEPSEEK_API_KEY")
        return 1

    strat = get_strategy(None)
    system_blocks = _system_prompt(strat)
    user_prompt = _mock_user_prompt()

    print(f"Running {len(matrix)} model(s) for {NAME} ({CODE})")
    print()

    results: list[dict] = []
    ts_label = datetime.now().strftime("%Y%m%d_%H%M%S")
    for label, transport, base_url, api_key, model in matrix:
        print(f"=== {label} ===")
        t0 = time.time()
        try:
            payload = _call_one(label, transport, base_url, api_key, model,
                                system_blocks, user_prompt)
            dur = time.time() - t0
            print(f"  ✓ {dur:.1f}s")
            results.append({
                "label": label,
                "model": model,
                "ok": True,
                "latency_s": dur,
                "payload": payload,
            })
            # Per-model JSON dump for ad-hoc inspection. Sanitize label
            # so it becomes a safe filename (no parens/spaces).
            safe = label.replace(" ", "_").replace("(", "").replace(")", "")
            json_path = f"/tmp/compare_{ts_label}_{safe}.json"
            with open(json_path, "w") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"    → {json_path}")
        except Exception as e:
            dur = time.time() - t0
            print(f"  ✗ failed after {dur:.1f}s: {e}")
            results.append({
                "label": label,
                "model": model,
                "ok": False,
                "latency_s": dur,
                "error": str(e),
            })

    report = _render_report(results)
    report_path = f"/tmp/compare_{ts_label}.md"
    with open(report_path, "w") as f:
        f.write(report)
    print()
    print(f"Report: {report_path}")
    print()
    print("Open in editor:  code " + report_path)
    print("Or render:       glow " + report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
