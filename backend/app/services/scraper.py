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

from .realtime_quotes import fetch_quotes_sina

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


def collect_one(code: str, sina_spot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect a snapshot dict for one code. Never raises — fields are None on failure.

    `sina_spot` lets callers pass a pre-fetched sina quote (one HTTP call
    for the whole watchlist via fetch_quotes_sina). When provided we skip
    the per-code akshare spot endpoint, which on Railway is unreachable
    for both Xueqiu (`'data'` KeyError) and em bid_ask (empty body).
    """
    spot = sina_spot if sina_spot else _spot(code)
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
    """Collect snapshots for all codes in parallel.

    Hits sina hq once for the whole batch (price/change/volume/turnover)
    so the per-code workers only handle fund flow + news + notices. Codes
    sina didn't cover (rare — usually delisted) fall back to akshare's
    per-code spot endpoint inside collect_one.
    """
    if not codes:
        return []
    sina = fetch_quotes_sina(codes)
    if sina:
        logger.info("collect_many: sina filled %d/%d codes", len(sina), len(codes))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(codes))) as pool:
        futures = {pool.submit(collect_one, c, sina.get(c)): c for c in codes}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                code = futures[fut]
                logger.error("collect_one(%s) failed: %s", code, e)
    return results


def collect_quotes_bulk(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Pull price/change/volume/turnover + main fund flow for many codes.

    Cascading source strategy, ordered by reliability on Railway today:

      1. **eastmoney bulk** (`stock_zh_a_spot_em` + `..._fund_flow_rank`).
         One round-trip if it works — but push2.eastmoney.com is
         consistently unreachable from Railway, so this almost always
         falls through. Kept in case the host comes back or we move infra.
      2. **sina hq direct** (hq.sinajs.cn). Single HTTP GET for the whole
         batch, no token, no third-party lib. Carries price/change/volume
         /turnover but not main_net_flow. This is the primary working
         path right now.
      3. **akshare per-code fan-out** (Xueqiu / em bid_ask + per-code
         fund_flow). Slow, partially failing, but the only place we can
         backfill main_net_flow.

    Whatever each layer fills in carries forward; later layers only run
    for codes still missing core data, and step 3 also runs for codes
    that have price but lack main_net_flow.

    Returns {code: {price, change_pct, volume, turnover, main_net_flow?}}
    for codes we got data for. Missing codes should not be written as new
    snapshot rows.
    """
    if not codes:
        return {}

    out: dict[str, dict[str, Any]] = {}

    # 1. Eastmoney bulk (cheap when it works).
    bulk = _try_bulk(codes)
    if bulk:
        out.update(bulk)

    # 2. Sina direct for codes that bulk didn't cover with price.
    needs_spot = [c for c in codes if out.get(c, {}).get("price") is None]
    if needs_spot:
        sina = fetch_quotes_sina(needs_spot)
        if sina:
            for c, q in sina.items():
                out.setdefault(c, {}).update(q)
            logger.info("quotes: sina filled %d/%d codes", len(sina), len(needs_spot))
        else:
            logger.warning("quotes: sina returned nothing for %d codes", len(needs_spot))

    # 3. akshare per-code fan-out: backfill anything still missing core
    # fields, plus main_net_flow for codes sina covered.
    still_missing = [c for c in codes if out.get(c, {}).get("price") is None]
    needs_flow = [
        c for c in codes
        if c in out and out[c].get("main_net_flow") is None
    ]
    if still_missing or needs_flow:
        # One thread pool for both — the work items are independent.
        per_code = _per_code_quotes(still_missing)
        for c, q in per_code.items():
            out.setdefault(c, {}).update(q)
        if needs_flow:
            flows = _per_code_flow_only(needs_flow)
            for c, flow in flows.items():
                if c in out:
                    out[c]["main_net_flow"] = flow

    return out


def _try_bulk(codes: list[str]) -> dict[str, dict[str, Any]]:
    code_set = set(codes)
    out: dict[str, dict[str, Any]] = {}

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
        flow_col = next(
            (c for c in ("今日主力净流入-净额", "主力净流入-净额") if c in sub.columns),
            None,
        )
        if flow_col is not None:
            for _, row in sub.iterrows():
                c = str(row["代码"])
                out.setdefault(c, {})["main_net_flow"] = _to_float(row.get(flow_col))

    return out


def _quotes_one(code: str) -> dict[str, Any]:
    """Per-code quotes fetch — same endpoints as the hourly full job uses
    successfully on Railway (Xueqiu spot + per-code eastmoney fund-flow)."""
    spot = _spot(code)
    return {**spot, "main_net_flow": _fund_flow(code)}


def _per_code_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Concurrent per-code fallback. Drops codes whose spot AND fund-flow
    both came back None — caller treats them as 'no data this tick'."""
    if not codes:
        return {}
    out: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(10, len(codes))) as pool:
        futures = {pool.submit(_quotes_one, c): c for c in codes}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                d = fut.result()
            except Exception as e:
                logger.warning("per-code quotes(%s) failed: %s", c, e)
                continue
            if any(v is not None for v in d.values()):
                out[c] = d
    return out


def _per_code_flow_only(codes: list[str]) -> dict[str, float]:
    """Lightweight version of _per_code_quotes that only fetches main fund
    flow — used to backfill main_net_flow for codes whose price came from
    sina (which doesn't carry flow)."""
    if not codes:
        return {}
    out: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=min(10, len(codes))) as pool:
        futures = {pool.submit(_fund_flow, c): c for c in codes}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                flow = fut.result()
            except Exception:
                flow = None
            if flow is not None:
                out[c] = flow
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
