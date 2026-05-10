"""LLM-driven daily sector recommendation pipeline.

Pipeline:
  1. Pull sina sector spot (cached 5 min) — 49 sectors with today's metrics
  2. Pick top N sectors by today's change_pct
  3. For each sector, pull 成份股 via akshare.stock_sector_detail (~20 stocks)
     and rank by today's changepercent — keep top 2K as candidates
  4. One LLM call returns N × K final picks with per-sector and per-stock
     reasons, structured via a forced tool_choice
  5. Persist to SectorPicks table (single-row replace) — TTL 2h checked
     in the route layer

Design notes:
- Per-sector candidate pool uses a 30-min in-memory cache because
  stock_sector_detail is the slowest step (~1s/sector × N) and the LLM
  call is the only thing that's expensive in $$. Caching keeps the
  user-facing latency for refresh under 30s.
- Single-row table is intentional: we always show "today's pick" — there's
  no historical browsing. Easier than a (date, sector) keyed table.
- LLM prompt feeds candidates with code+name+chg%+PE+turnover so it can
  pick on more than just "biggest gainer today".
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import akshare as ak
from anthropic import Anthropic
from sqlalchemy.orm import Session

from ..config import settings
from ..models import SectorPicks
from .sectors import get_sectors

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "kimi-k2.5"

# How many top sectors to recommend, and how many picks per sector.
TOP_N_SECTORS = 5
PICKS_PER_SECTOR = 3
# We hand the LLM a wider candidate pool so it has room to pick on more
# than just "biggest mover" — some sectors lead with a meme stock.
CANDIDATES_PER_SECTOR = 8

# Per-sector stock list cache. Keyed by sector label (e.g. "new_blhy").
_stocks_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_STOCKS_CACHE_TTL_SECONDS = 30 * 60
_lock = threading.RLock()


def _strip_exchange_prefix(s: str) -> str:
    """sina returns 'sh600519' / 'sz000001' — peel the 2-letter prefix."""
    s = (s or "").strip()
    if len(s) >= 8 and s[:2] in ("sh", "sz", "bj"):
        return s[2:]
    return s


def _fetch_sector_stocks(label: str, top_k: int = CANDIDATES_PER_SECTOR) -> list[dict[str, Any]]:
    """Pull 成份股 for one sector. Returns top-k by today's changepercent.

    Cached 30 min in-process; same label hit again within window short-circuits.
    Returns [] on any akshare error so the caller can skip the sector
    rather than aborting the whole pick run.
    """
    now = time.time()
    with _lock:
        cached = _stocks_cache.get(label)
        if cached and now - cached[0] < _STOCKS_CACHE_TTL_SECONDS:
            return cached[1]
    try:
        df = ak.stock_sector_detail(sector=label)
    except Exception:
        logger.exception("sector_picks: stock_sector_detail failed for %s", label)
        return []
    if df is None or len(df) == 0:
        return []
    # Sort by today's gain — the LLM's job is to pick from the obvious
    # heat-of-the-day candidates plus reason about why each is or isn't
    # a good idea. Keep PE/turnover so it can balance "hot" vs "fundamentals".
    try:
        df_sorted = df.sort_values("changepercent", ascending=False).head(top_k)
    except Exception:
        df_sorted = df.head(top_k)
    rows: list[dict[str, Any]] = []
    for _, r in df_sorted.iterrows():
        try:
            rows.append({
                "code": _strip_exchange_prefix(str(r.get("code") or r.get("symbol") or "")),
                "name": str(r.get("name") or "").strip(),
                "change_pct": float(r.get("changepercent") or 0),
                "price": float(r.get("trade") or 0) or None,
                "pe_ratio": float(r.get("per") or 0) or None,
                "pb_ratio": float(r.get("pb") or 0) or None,
                "turnover_rate": float(r.get("turnoverratio") or 0) or None,
            })
        except Exception:
            continue
    with _lock:
        _stocks_cache[label] = (now, rows)
    return rows


# LLM tool schema. forced tool_choice → kimi has to call it once with the
# structured payload; we read the input back as our pick list.
PICKS_TOOL = {
    "name": "submit_sector_picks",
    "description": "提交今日 A 股板块推荐 + 每板块的具体推荐股票。必须调用一次。",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["sectors"],
        "properties": {
            "sectors": {
                "type": "array",
                "minItems": 1,
                "maxItems": TOP_N_SECTORS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "change_pct", "reason", "picks"],
                    "properties": {
                        "name": {"type": "string"},
                        "change_pct": {"type": "number"},
                        "reason": {
                            "type": "string",
                            "description": (
                                "≤80 字。说明今日为什么值得关注这个板块（资金/题材/业绩/政策），"
                                "末尾用「但…」一句话点出风险。不要复读涨跌幅数字。"
                            ),
                        },
                        "picks": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": PICKS_PER_SECTOR,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["code", "name", "reason"],
                                "properties": {
                                    "code": {
                                        "type": "string",
                                        "pattern": r"^\d{6}$",
                                        "description": "必须从候选 list 里选；不要编造代码。",
                                    },
                                    "name": {"type": "string"},
                                    "reason": {
                                        "type": "string",
                                        "description": (
                                            "≤40 字。为什么是这一支：估值/业绩/资金/题材里至少踩一项，"
                                            "克制具体不要喊口号。"
                                        ),
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


def _system_prompt() -> str:
    return (
        "你是 A 股盯盘助手，性格克制的老研究员风格。任务：基于今日板块涨跌 + 成份股数据，"
        "选 TOP{n} 个值得关注的板块，并在每个板块里挑 {k} 支具体推荐股票，给出"
        "结构化的简短理由。\n\n"
        "硬约束：\n"
        "- 板块顺序按今日涨跌幅降序保留\n"
        "- 每支推荐股票的代码必须出自候选列表，不要编造\n"
        "- 板块 reason ≤80 字；股票 reason ≤40 字\n"
        "- 不要全篇空话，避免「龙头股」「优质标的」这种没信息量的词\n"
        "- 风险点必须显式提到：板块层面用「但…」收尾；股票层面如有个股风险也点出\n"
    ).format(n=TOP_N_SECTORS, k=PICKS_PER_SECTOR)


def _build_user_prompt(sectors: list[dict[str, Any]]) -> str:
    """Render the top sectors + per-sector candidate stocks as a markdown
    block the LLM can scan."""
    lines: list[str] = ["## 今日 TOP 板块 + 候选成份股", ""]
    for i, sec in enumerate(sectors, 1):
        lines.append(
            f"### {i}. {sec['name']} (label={sec['label']}) "
            f"今日 {sec['change_pct']:+.2f}% · 家数 {sec.get('company_count', '?')}"
        )
        cands = sec.get("candidates") or []
        if not cands:
            lines.append("（成份股拉取失败，跳过个股推荐）")
            lines.append("")
            continue
        lines.append("候选股票：")
        for c in cands:
            metrics = []
            if c.get("pe_ratio") is not None:
                metrics.append(f"PE {c['pe_ratio']:.1f}")
            if c.get("pb_ratio") is not None:
                metrics.append(f"PB {c['pb_ratio']:.2f}")
            if c.get("turnover_rate") is not None:
                metrics.append(f"换手 {c['turnover_rate']:.1f}%")
            metrics_str = " · ".join(metrics) if metrics else "—"
            lines.append(
                f"- {c['code']} {c['name']}  {c['change_pct']:+.2f}%  {metrics_str}"
            )
        lines.append("")
    lines.append("请基于上述数据调用 submit_sector_picks 一次，给出推荐。")
    return "\n".join(lines)


def compute_picks(client: Anthropic | None = None) -> dict[str, Any]:
    """Run the full pick pipeline and return the structured payload.
    Does NOT persist — caller writes to DB."""
    all_sectors = get_sectors()
    if not all_sectors:
        raise RuntimeError("sector spot returned empty list")
    top = all_sectors[:TOP_N_SECTORS]

    # Fan out per-sector candidate fetches. Single-threaded for now; sina is
    # ~1s/call so 5 calls ≈ 5s. If the latency hurts later, ThreadPool it.
    enriched: list[dict[str, Any]] = []
    for s in top:
        label = s.get("code") or ""
        cands = _fetch_sector_stocks(label) if label else []
        enriched.append({
            "name": s["name"],
            "label": label,
            "change_pct": s.get("change_pct") or 0.0,
            "company_count": s.get("company_count"),
            "candidates": cands,
        })

    # Drop sectors with zero candidates so the LLM doesn't hallucinate codes
    usable = [s for s in enriched if s["candidates"]]
    if not usable:
        raise RuntimeError("no sector returned any candidate stocks")

    if client is None:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        kwargs: dict[str, Any] = {"api_key": settings.ANTHROPIC_API_KEY}
        if settings.ANTHROPIC_BASE_URL:
            kwargs["base_url"] = settings.ANTHROPIC_BASE_URL
        client = Anthropic(**kwargs)

    model = settings.ANALYSIS_MODEL or DEFAULT_MODEL
    base_kwargs = {
        "model": model,
        "max_tokens": 4096,
        "system": _system_prompt(),
        "tools": [PICKS_TOOL],
        "messages": [{"role": "user", "content": _build_user_prompt(usable)}],
    }
    try:
        msg = client.messages.create(
            **base_kwargs,
            tool_choice={"type": "tool", "name": "submit_sector_picks"},
        )
    except Exception as e:
        if "tool_choice" in str(e) or "400" in str(e):
            logger.info("model %s rejected forced tool_choice; retrying with 'any'", model)
            msg = client.messages.create(**base_kwargs, tool_choice={"type": "any"})
        else:
            raise

    tool_use = next((b for b in msg.content if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError("model did not return a tool_use block")
    payload: dict[str, Any] = tool_use.input  # type: ignore[assignment]
    if "sectors" not in payload:
        raise RuntimeError(f"unexpected tool input: {json.dumps(payload)[:200]}")

    # Validate that picked codes were actually in the candidate set per
    # sector. If model hallucinated a code we drop the bad pick rather
    # than failing — better to show 2 picks than zero.
    by_sector_cands = {s["name"]: {c["code"] for c in s["candidates"]} for s in usable}
    cleaned_sectors: list[dict[str, Any]] = []
    for sec in payload.get("sectors", []):
        valid_codes = by_sector_cands.get(sec.get("name"), set())
        clean_picks = [
            p for p in sec.get("picks", [])
            if isinstance(p, dict) and p.get("code") in valid_codes
        ]
        if not clean_picks:
            continue
        sec["picks"] = clean_picks
        cleaned_sectors.append(sec)

    return {
        "sectors": cleaned_sectors,
        "model": model,
    }


def get_or_compute(db: Session, max_age_seconds: int, force: bool = False) -> dict[str, Any]:
    """Return cached SectorPicks payload + meta, regenerating if needed.

    Result shape: {sectors, generated_at, model, is_fresh}. is_fresh tells
    the frontend whether to show a "regenerate" CTA prominently.
    """
    row = db.query(SectorPicks).filter(SectorPicks.id == 1).first()
    now = datetime.now(timezone.utc)

    if row is not None and not force:
        ga = row.generated_at
        if ga.tzinfo is None:
            ga = ga.replace(tzinfo=timezone.utc)
        age = (now - ga).total_seconds()
        if age < max_age_seconds:
            return {
                **row.payload,
                "generated_at": ga.isoformat(),
                "model": row.model,
                "is_fresh": True,
            }

    fresh = compute_picks()
    if row is None:
        row = SectorPicks(id=1, payload=fresh, model=fresh["model"])
        db.add(row)
    else:
        row.payload = fresh
        row.model = fresh["model"]
        row.generated_at = now
    db.commit()
    return {
        **fresh,
        "generated_at": now.isoformat(),
        "is_fresh": True,
    }
