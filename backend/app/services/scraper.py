"""Per-code A-share data collection via akshare.

Each call returns "best effort" data — if a particular akshare endpoint times
out or fails, we log and return None for that field rather than fail the
entire snapshot. The cron loop catches one bad stock without breaking the
batch.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import akshare as ak

logger = logging.getLogger(__name__)

# Important notice keywords — used by the signals engine and as filter for the
# notices feed shown in the 盯盘 view.
NOTICE_KEYWORDS = (
    "业绩", "重组", "并购", "重大", "停牌", "复牌", "减持", "增持",
    "回购", "分红", "退市", "立案", "诉讼", "终止", "中标",
)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning("akshare call failed: %s(%s) → %s", fn.__name__, args, e)
        return None


def _spot(code: str) -> dict[str, float | None]:
    """Latest price/change/volume/turnover for a single code."""
    df = _safe(ak.stock_individual_spot_xq, symbol=_xq_symbol(code))
    if df is None or len(df) == 0:
        # Fallback to bid endpoint
        df = _safe(ak.stock_bid_ask_em, symbol=code)
        if df is None or len(df) == 0:
            return {"price": None, "change_pct": None, "volume": None, "turnover": None}

    # Both endpoints return a 2-col (item, value) shape.
    by_item = dict(zip(df["item"].astype(str), df["value"]))
    def _f(k):
        v = by_item.get(k)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "price": _f("最新") or _f("最新价"),
        "change_pct": _f("涨幅") or _f("涨跌幅"),
        "volume": _f("成交量"),
        "turnover": _f("成交额"),
    }


def _xq_symbol(code: str) -> str:
    """Xueqiu requires SH/SZ prefix in caps."""
    if code.startswith(("60", "68")):
        return f"SH{code}"
    if code.startswith(("00", "30")):
        return f"SZ{code}"
    if code.startswith(("8", "4")):
        return f"BJ{code}"
    return code


def _fund_flow(code: str) -> float | None:
    """Today's main-force net inflow (元)."""
    df = _safe(
        ak.stock_individual_fund_flow,
        stock=code,
        market="sh" if code.startswith(("60", "68")) else "sz",
    )
    if df is None or len(df) == 0:
        return None
    # Most recent row; the column is "主力净流入-净额" (元).
    try:
        latest = df.iloc[-1]
        for col in ("主力净流入-净额", "主力净流入"):
            if col in latest.index:
                v = latest[col]
                return float(v) if v is not None else None
    except Exception:
        return None
    return None


def _news(code: str, limit: int = 5) -> list[dict[str, Any]]:
    df = _safe(ak.stock_news_em, symbol=code)
    if df is None or len(df) == 0:
        return []
    out = []
    for _, row in df.head(limit).iterrows():
        out.append({
            "title": str(row.get("新闻标题") or row.get("标题") or "").strip(),
            "url": str(row.get("新闻链接") or row.get("链接") or "").strip(),
            "ts": str(row.get("发布时间") or "").strip(),
        })
    return [n for n in out if n["title"]]


def _notices(code: str, limit: int = 5) -> list[dict[str, Any]]:
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    df = _safe(ak.stock_notice_report, symbol="全部", date=today)
    if df is None or len(df) == 0:
        return []
    # Filter to this code
    if "代码" in df.columns:
        df = df[df["代码"].astype(str) == code]
    elif "股票代码" in df.columns:
        df = df[df["股票代码"].astype(str).str.endswith(code)]
    else:
        return []
    out = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get("公告标题") or row.get("标题") or "").strip()
        out.append({
            "title": title,
            "url": str(row.get("公告链接") or "").strip(),
            "ts": str(row.get("公告日期") or row.get("发布日期") or "").strip(),
            "type": _classify_notice(title),
        })
    return [n for n in out if n["title"]]


def _classify_notice(title: str) -> str | None:
    for kw in NOTICE_KEYWORDS:
        if kw in title:
            return kw
    return None


def collect_one(code: str) -> dict[str, Any]:
    """Collect a snapshot dict for one code. Never raises — fields are None on failure."""
    spot = _spot(code)
    fund = _fund_flow(code)
    news = _news(code)
    notices = _notices(code)
    return {
        "code": code,
        **spot,
        "main_net_flow": fund,
        "north_hold_change": None,  # Phase 2.5 (needs an extra bulk call we skip for MVP)
        "news": news,
        "notices": notices,
        "lhb": None,  # filled by post-close LHB pass
    }


def collect_many(codes: list[str], max_workers: int = 10) -> list[dict[str, Any]]:
    """Collect snapshots for all codes in parallel."""
    if not codes:
        return []
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(codes))) as pool:
        futures = {pool.submit(collect_one, c): c for c in codes}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                code = futures[fut]
                logger.error("collect_one(%s) failed: %s", code, e)
    return results


def collect_quotes_bulk(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Pull price/change/volume/turnover + main fund flow for many codes in
    two bulk akshare calls — one for the whole spot table, one for the whole
    fund-flow rank. Used by the high-frequency quotes-only cron.

    Returns {code: {price, change_pct, volume, turnover, main_net_flow}}.
    Codes that aren't in the bulk responses (e.g., 北交所 sometimes drops
    out, or akshare hiccups) come back missing — caller decides what to do.

    Why not stay on per-code endpoints: at 20+ codes the per-code spot
    endpoint (Xueqiu) starts rate-limiting, leaving rows with – – in the UI.
    The bulk eastmoney endpoint returns the whole market in one shot.
    """
    if not codes:
        return {}
    code_set = set(codes)
    out: dict[str, dict[str, Any]] = {c: {} for c in codes}

    spot_df = _safe(ak.stock_zh_a_spot_em)
    if spot_df is not None and len(spot_df) > 0 and "代码" in spot_df.columns:
        sub = spot_df[spot_df["代码"].astype(str).isin(code_set)]
        for _, row in sub.iterrows():
            c = str(row["代码"])
            out.setdefault(c, {}).update({
                "price": _to_float(row.get("最新价")),
                "change_pct": _to_float(row.get("涨跌幅")),
                "volume": _to_float(row.get("成交量")),
                "turnover": _to_float(row.get("成交额")),
            })

    flow_df = _safe(ak.stock_individual_fund_flow_rank, indicator="今日")
    if flow_df is not None and len(flow_df) > 0 and "代码" in flow_df.columns:
        sub = flow_df[flow_df["代码"].astype(str).isin(code_set)]
        # The column is "今日主力净流入-净额" (元).
        flow_col = next(
            (c for c in ("今日主力净流入-净额", "主力净流入-净额") if c in sub.columns),
            None,
        )
        if flow_col is not None:
            for _, row in sub.iterrows():
                c = str(row["代码"])
                out.setdefault(c, {})["main_net_flow"] = _to_float(row.get(flow_col))

    return out


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # akshare uses NaN for missing cells; treat them the same as None upstream.
    if f != f:  # NaN check without importing math
        return None
    return f


def collect_lhb_today() -> dict[str, dict[str, Any]]:
    """Pull today's 龙虎榜 list, return {code: lhb_info}."""
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    df = _safe(ak.stock_lhb_detail_em, start_date=today, end_date=today)
    if df is None or len(df) == 0:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        code = str(row.get("代码") or "").strip()
        if not re.match(r"^\d{6}$", code):
            continue
        out[code] = {
            "name": str(row.get("名称") or "").strip(),
            "reason": str(row.get("上榜原因") or "").strip(),
            "net_buy": float(row.get("龙虎榜净买额") or 0) or None,
        }
    return out
