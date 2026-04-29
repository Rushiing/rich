"""Per-code A-share data collection via akshare.

Each call returns "best effort" data — if a particular akshare endpoint times
out or fails, we log and return None for that field rather than fail the
entire snapshot. The cron loop catches one bad stock without breaking the
batch.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

import akshare as ak

from .realtime_quotes import fetch_quotes_sina, fetch_quotes_tencent

# Fields beyond the basic quote — Tencent carries them, sina and akshare don't.
# Listed here so the cascade and Snapshot insertion stay in sync.
VALUATION_FIELDS = ("pe_ratio", "pb_ratio", "turnover_rate", "market_cap", "circ_market_cap")

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


def _safe_with_timeout(fn, *args, _timeout: float = 8.0, **kwargs):
    """Run `fn` with a hard wall-time cap.

    Why we need this *on top of* requests.Session.send's 12s default:
    akshare's news/notice helpers paginate internally, each page being a
    fresh HTTP call within the 12s budget — so the read timer never trips
    even though cumulative time can run minutes. Wrapping the whole helper
    in a thread future lets us bail out at a true wall-clock deadline.

    The inner thread leaks if it's stuck in I/O (Python can't kill it),
    but it'll exit on its own when akshare eventually gives up. We don't
    block on it.
    """
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="akshare-bounded")
    try:
        fut = pool.submit(_safe, fn, *args, **kwargs)
        try:
            return fut.result(timeout=_timeout)
        except FuturesTimeout:
            logger.warning("akshare call %s exceeded %ss, abandoning",
                           fn.__name__, _timeout)
            return None
    finally:
        pool.shutdown(wait=False)


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
    # 8s wall-time cap: akshare's stock_news_em internally paginates the
    # entire history of a stock; for heavy-news names (300442 润泽科技,
    # 603993 洛阳钼业 etc.) that loops through tens of pages and never
    # trips requests's 12s read timer. We just want the most recent few
    # titles for the LLM prompt — if 8s isn't enough, treat as missing.
    df = _safe_with_timeout(ak.stock_news_em, symbol=code, _timeout=8.0)
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
    # Same wall-time cap reasoning as _news: stock_notice_report fetches
    # all market announcements for the day with internal pagination.
    df = _safe_with_timeout(
        ak.stock_notice_report, symbol="全部", date=today, _timeout=8.0,
    )
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


def collect_one(code: str, bulk_spot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Collect a snapshot dict for one code. Never raises — fields are None on failure.

    `bulk_spot` lets callers pass a pre-fetched quote from the bulk source
    (Tencent or sina, fetched once for the whole watchlist). When provided
    we skip the per-code akshare spot endpoint — on Railway Xueqiu and em
    bid_ask are unreachable so this fallback is rarely needed. Tencent
    bulk also carries valuation fields (PE/PB/换手率/市值) which sina and
    akshare don't, so passing it through is how those land in the hourly
    snapshot too.
    """
    spot = bulk_spot if bulk_spot else _spot(code)
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

    Hits Tencent qt.gtimg.cn once for the whole batch (price/change/volume/
    turnover + valuation). Codes Tencent didn't cover fall back to sina hq
    in a second batch call. Per-code workers then handle fund flow + news +
    notices. Anything still missing core spot data falls through to akshare
    per-code inside collect_one.
    """
    if not codes:
        return []
    bulk: dict[str, dict[str, Any]] = {}
    tencent = fetch_quotes_tencent(codes)
    if tencent:
        bulk.update(tencent)
        logger.info("collect_many: tencent filled %d/%d codes", len(tencent), len(codes))

    needs_sina = [c for c in codes if c not in bulk]
    if needs_sina:
        sina = fetch_quotes_sina(needs_sina)
        if sina:
            for c, q in sina.items():
                bulk.setdefault(c, {}).update(q)
            logger.info("collect_many: sina filled %d/%d remaining codes",
                        len(sina), len(needs_sina))

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(codes))) as pool:
        futures = {pool.submit(collect_one, c, bulk.get(c)): c for c in codes}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                code = futures[fut]
                logger.error("collect_one(%s) failed: %s", code, e)
    return results


def collect_quotes_bulk(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Pull price/change/volume/turnover + valuation + main fund flow for many codes.

    Cascading source strategy, ordered by reliability + richness on Railway:

      1. **Tencent qt.gtimg.cn** (NEW). Single HTTP GET for the whole batch.
         Carries everything sina has *plus* PE / PB / 换手率 / 总市值 / 流通市值
         in the same payload. Promoted to primary because the extra fields
         materially help the LLM analysis prompt.
      2. **Sina hq direct** (hq.sinajs.cn). Same shape, basic fields only.
         Falls in for codes Tencent didn't cover (rare) or as a backup if
         Tencent is down.
      3. **eastmoney bulk** (`stock_zh_a_spot_em` + `..._fund_flow_rank`).
         push2.eastmoney.com is consistently unreachable from Railway, so
         this almost always no-ops. Kept for the rank-based main_net_flow
         in case the host comes back.
      4. **akshare per-code fan-out** (Xueqiu / em bid_ask + per-code
         fund_flow). Slow, partially failing, but the only path that
         reliably backfills main_net_flow on Railway today.

    Each layer fills only what's still missing; layer 4 also fills
    main_net_flow for codes earlier layers covered without flow data.

    Returns {code: {price, change_pct, volume, turnover, main_net_flow?,
    pe_ratio?, pb_ratio?, turnover_rate?, market_cap?, circ_market_cap?}}.
    """
    if not codes:
        return {}

    out: dict[str, dict[str, Any]] = {}

    # 1. Tencent — primary because of the valuation fields.
    tencent = fetch_quotes_tencent(codes)
    if tencent:
        out.update(tencent)
        logger.info("quotes: tencent filled %d/%d codes", len(tencent), len(codes))

    # 2. Sina for codes Tencent didn't return.
    needs_spot = [c for c in codes if out.get(c, {}).get("price") is None]
    if needs_spot:
        sina = fetch_quotes_sina(needs_spot)
        if sina:
            for c, q in sina.items():
                out.setdefault(c, {}).update(q)
            logger.info("quotes: sina filled %d/%d remaining codes",
                        len(sina), len(needs_spot))

    # 3. eastmoney bulk — hopefully picks up main_net_flow if push2 is reachable.
    bulk = _try_bulk(codes)
    if bulk:
        for c, q in bulk.items():
            for k, v in q.items():
                out.setdefault(c, {}).setdefault(k, v)

    # 4. akshare per-code: anything still missing core fields, plus
    # main_net_flow for codes that have a price but no flow.
    still_missing = [c for c in codes if out.get(c, {}).get("price") is None]
    needs_flow = [
        c for c in codes
        if c in out and out[c].get("main_net_flow") is None
    ]
    if still_missing or needs_flow:
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


def _per_code_quotes(codes: list[str], total_timeout: float = 60.0) -> dict[str, dict[str, Any]]:
    """Concurrent per-code fallback. Drops codes whose spot AND fund-flow
    both came back None — caller treats them as 'no data this tick'.

    A wall-time ceiling is critical here: this used to be `with
    ThreadPoolExecutor + as_completed(no timeout)`, which would deadlock
    forever if a single akshare call escaped its 12s read timer (e.g.,
    a slow byte-stream that keeps resetting the timer). When that
    happened the entire quotes_tick hung, APScheduler refused all
    subsequent quotes ticks with "max running instances reached", and
    the 盯盘 list went stale until next deploy.
    """
    if not codes:
        return {}
    return _bounded_pool(_quotes_one, codes, total_timeout, _quotes_accepts)


def _per_code_flow_only(codes: list[str], total_timeout: float = 60.0) -> dict[str, float]:
    """Lightweight version of _per_code_quotes — only main fund flow.
    Same wall-time guard reasoning as `_per_code_quotes`."""
    if not codes:
        return {}
    return _bounded_pool(_fund_flow, codes, total_timeout, _flow_accepts)


def _quotes_accepts(d: Any) -> bool:
    return isinstance(d, dict) and any(v is not None for v in d.values())


def _flow_accepts(v: Any) -> bool:
    return v is not None


def _bounded_pool(fn, codes: list[str], total_timeout: float, accepts) -> dict[str, Any]:
    """Run `fn(code)` for each code in a bounded thread pool with a
    wall-time deadline. Returns whatever finished by the deadline; codes
    still in flight are abandoned (their threads keep running until akshare
    eventually gives up, but the caller doesn't wait).
    """
    out: dict[str, Any] = {}
    pool = ThreadPoolExecutor(
        max_workers=min(10, len(codes)), thread_name_prefix="quotes-bounded",
    )
    futures = {pool.submit(fn, c): c for c in codes}
    try:
        for fut in as_completed(futures, timeout=total_timeout):
            c = futures[fut]
            try:
                v = fut.result()
            except Exception as e:
                logger.warning("%s(%s) failed: %s", fn.__name__, c, e)
                continue
            if accepts(v):
                out[c] = v
    except FuturesTimeout:
        unfinished = sum(1 for f in futures if not f.done())
        logger.warning(
            "%s: %d/%d codes still in flight after %ss; abandoning so the "
            "next tick can run",
            fn.__name__, unfinished, len(codes), total_timeout,
        )
        for f in futures:
            if not f.done():
                f.cancel()  # best-effort; can't kill a thread mid-I/O
    pool.shutdown(wait=False)  # don't block return on stuck workers
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
