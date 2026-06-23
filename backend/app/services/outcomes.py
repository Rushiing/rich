"""Analysis-outcome tracking — the feedback loop.

Each time an analysis is generated we drop an anchor row (verdict +
reference price). The daily cron then fills in forward closing prices at
+1 / +3 / +5 / +20 trading days, so over time we can answer "did the
建议买入 calls actually go up?" — and compare hit rate across prompt
versions / single-vs-debate modes.

Forward prices come from the `klines` table (daily qfq close), so "N
trading days later" is just "the Nth kline row dated after generated_at".
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import AnalysisOutcome, Kline

logger = logging.getLogger(__name__)

# The day-offsets we track. Keep in sync with the close_dN / return_dN
# columns on AnalysisOutcome.
HORIZONS = [1, 3, 5, 20]


def record_anchor(
    db: Session, code: str, generated_at: datetime, actionable: str,
    prompt_version: str | None, mode: str | None, anchor_price: float | None,
    confidence: int | None = None,
    data_completeness: int | None = None,
    model: str | None = None,
    nd_trend: str | None = None,
    nd_confidence: str | None = None,
    cohort: str | None = None,
    buy_low: float | None = None,
    buy_high: float | None = None,
    target_low: float | None = None,
    stop_price: float | None = None,
) -> None:
    """Insert an outcome anchor. Called from analysis.generate() right
    after the Analysis row is persisted. No-op when anchor_price is
    missing — without a reference price we can't measure return.

    5/29: also stores confidence + data_completeness per anchor so the
    detail-page "历史解析" card can show how those values evolved
    across regenerations. Both default to None for call sites that
    haven't been updated (e.g. unit tests, batch backfills).

    6/10: model (A/B bucketing) + nd_trend/nd_confidence (next_day_outlook
    scoring — see nd_outlook_stats). All optional, None for legacy sites.

    6/23 (P0): buy_low/buy_high/target_low/stop_price — the LLM's price
    predictions (买入区/目标区/最紧止损), captured at anchor time so
    price_level_stats can later score them against forward klines. All
    default None so old call sites stay unaffected."""
    if anchor_price is None or anchor_price <= 0:
        logger.info("outcome anchor skipped for %s — no anchor price", code)
        return
    # Normalize legacy enum confidence ("高"/"中"/"低") to int — anchor
    # rows should be uniformly numeric so the history table doesn't
    # have to render mixed types.
    if isinstance(confidence, str):
        confidence = {"高": 85, "中": 65, "低": 45}.get(confidence)
    db.add(AnalysisOutcome(
        code=code,
        generated_at=generated_at,
        actionable=actionable or "",
        prompt_version=prompt_version,
        mode=mode,
        anchor_price=anchor_price,
        confidence=confidence,
        data_completeness=data_completeness,
        model=model,
        nd_trend=nd_trend,
        nd_confidence=nd_confidence,
        cohort=cohort,
        buy_low=buy_low,
        buy_high=buy_high,
        target_low=target_low,
        stop_price=stop_price,
    ))
    db.commit()


def backfill_outcomes() -> dict:
    """Walk outcomes with unfilled horizons, fill close_dN / return_dN from
    the klines table. Idempotent — only fills columns that are still NULL
    and have enough trading days elapsed. Returns counters."""
    db: Session = SessionLocal()
    filled = scanned = 0
    try:
        # Rows with at least one unfilled horizon, or missing the
        # anchor_close basis (legacy rows predating 6/10).
        from sqlalchemy import or_
        rows = (
            db.query(AnalysisOutcome)
            .filter(or_(
                AnalysisOutcome.close_d20.is_(None),
                AnalysisOutcome.anchor_close.is_(None),
            ))
            .all()
        )
        for o in rows:
            scanned += 1
            gen_date = o.generated_at
            if gen_date.tzinfo is None:
                gen_date = gen_date.replace(tzinfo=timezone.utc)
            gen_day = gen_date.date().isoformat()

            changed = False

            # anchor_close: qfq close of the anchor's trading day (latest
            # kline ≤ gen_day — falls back to the prior trading day when
            # generated on a weekend/holiday). Same price series as
            # close_dN, so returns computed from it are dividend-safe,
            # unlike anchor_price (unadjusted intraday).
            if o.anchor_close is None:
                anchor_bar = (
                    db.query(Kline)
                    .filter(Kline.code == o.code, Kline.date <= gen_day)
                    .order_by(Kline.date.desc())
                    .first()
                )
                if anchor_bar is not None and anchor_bar.close is not None:
                    o.anchor_close = anchor_bar.close
                    changed = True

            # Trading days strictly after the generation date, ascending.
            future = (
                db.query(Kline)
                .filter(Kline.code == o.code, Kline.date > gen_day)
                .order_by(Kline.date.asc())
                .all()
            )
            for h in HORIZONS:
                close_attr = f"close_d{h}"
                return_attr = f"return_d{h}"
                if getattr(o, close_attr) is not None:
                    continue  # already filled
                if len(future) < h:
                    continue  # not enough trading days yet
                bar = future[h - 1]
                if bar.close is None:
                    continue
                # 复权安全(codex 审计修正):return 基准用 anchor_close(qfq
                # 锚点日收盘),跟前向 bar.close(同为 qfq)同一复权基准;
                # anchor_close 缺失(老行)才回退 anchor_price。原来拿未复权的
                # anchor_price 比 qfq 的 bar.close,跨除权日会系统性扭曲收益。
                basis = o.anchor_close if o.anchor_close is not None else o.anchor_price
                setattr(o, close_attr, bar.close)
                setattr(o, return_attr, (bar.close - basis) / basis * 100.0)
                changed = True
            if changed:
                o.updated_at = datetime.now(timezone.utc)
                filled += 1
        db.commit()
    finally:
        db.close()
    logger.info("outcomes backfill: scanned=%d filled=%d", scanned, filled)
    return {"scanned": scanned, "filled": filled}


def recompute_returns_from_close() -> dict:
    """一次性修正(codex 审计):历史 return_dN 是用未复权 anchor_price 当基准
    算的(bug),重算成用 anchor_close(qfq 复权安全),跟 close_dN 同基准。

    只动「有 anchor_close 且对应 close_dN 已填」的行;老行(无 anchor_close)
    无复权基准、留原值。幂等(同输入重跑同结果)。

    返回里带 d5 收益的「改了多少行 + 平均绝对变化(pp) + 最大变化」—— 用来
    判断这个 bug 到底有多大影响:多数票 5 日内无除权,delta≈0;只有跨除权
    日的票会显著移动。买入超额是否仍成立,以重算后重跑 hit_rate_stats 为准。"""
    db: Session = SessionLocal()
    scanned = changed = 0
    n_d5 = 0
    sum_abs_d5 = 0.0
    max_abs_d5 = 0.0
    try:
        rows = (
            db.query(AnalysisOutcome)
            .filter(AnalysisOutcome.anchor_close.isnot(None))
            .all()
        )
        for o in rows:
            scanned += 1
            basis = o.anchor_close
            if basis is None or basis <= 0:
                continue
            row_changed = False
            for h in HORIZONS:
                cval = getattr(o, f"close_d{h}")
                if cval is None:
                    continue
                new_ret = (cval - basis) / basis * 100.0
                old_ret = getattr(o, f"return_d{h}")
                if old_ret is None or abs(new_ret - old_ret) > 1e-9:
                    if h == 5 and old_ret is not None:
                        d = abs(new_ret - old_ret)
                        sum_abs_d5 += d
                        max_abs_d5 = max(max_abs_d5, d)
                        n_d5 += 1
                    setattr(o, f"return_d{h}", new_ret)
                    row_changed = True
            if row_changed:
                o.updated_at = datetime.now(timezone.utc)
                changed += 1
        db.commit()
    finally:
        db.close()
    avg_abs_d5 = round(sum_abs_d5 / n_d5, 3) if n_d5 else 0.0
    logger.info(
        "recompute returns from anchor_close: scanned=%d changed=%d "
        "d5_moved=%d avg_abs_d5=%.3f max_abs_d5=%.3f",
        scanned, changed, n_d5, avg_abs_d5, max_abs_d5,
    )
    return {
        "scanned": scanned, "changed": changed,
        "d5_rows_moved": n_d5,
        "avg_abs_d5_delta_pct": avg_abs_d5,
        "max_abs_d5_delta_pct": round(max_abs_d5, 3),
    }


def _gen_day(o: AnalysisOutcome) -> str:
    d = o.generated_at
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.date().isoformat()


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _is_hit(actionable: str, ret: float) -> bool:
    if actionable == "建议买入":
        return ret > 0
    if actionable == "建议卖出":
        return ret < 0
    return False


def hit_rate_stats() -> dict[str, Any]:
    """Compute hit-rate summary grouped by actionable verdict + prompt
    version. A 'hit' for 建议买入 = return_d5 > 0; for 建议卖出 =
    return_d5 < 0; others are not scored (no directional claim).

    6/10 honesty pass — two systematic biases in the raw numbers are now
    surfaced instead of hidden:

    - excess_return_d5: raw avg_return conflates skill with market beta
      (in a falling tape every 卖出 "hits"). Baseline = same-generation-day
      median return_d5 across ALL scored anchors (any verdict) — i.e. "the
      watchlist that day". Per-anchor excess = return − baseline; we report
      the bucket average. Positive excess on 买入 / negative on 卖出 is
      skill the market can't explain.
    - n_unique + hit_rate_dedup: the smart cron re-anchors the same stock
      on every 1.5% move, so one trending stock can contribute dozens of
      correlated "hits". Dedup keeps the LAST anchor per (code, day) —
      end-of-day verdict — and recomputes the hit rate on that set. The
      gap between hit_rate and hit_rate_dedup is the clustering inflation.

    Returns a dict the diag endpoint serializes directly."""
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(AnalysisOutcome)
            .filter(AnalysisOutcome.return_d5.isnot(None))
            .all()
        )
    finally:
        db.close()

    # Same-day, same-market baseline: median return_d5 of all scored anchors
    # that day *within the same board* (主板/科创/创业/北交). 科创板 ±20%
    # 波动若混进主板基线会带偏主板超额,故按 (day, market) 隔离 — 主板只跟
    # 主板比、科创只跟科创比。市场按 code 前缀实时派生,无 DB 列。
    from .stocks import market_board
    by_seg: dict[tuple, list[float]] = {}
    for o in rows:
        by_seg.setdefault((_gen_day(o), market_board(o.code)), []).append(o.return_d5)
    seg_baseline = {k: _median(vals) for k, vals in by_seg.items()}

    # Dedup set: last anchor per (code, gen_day).
    last_per_code_day: dict[tuple, AnalysisOutcome] = {}
    for o in rows:
        key = (o.code, _gen_day(o))
        cur = last_per_code_day.get(key)
        if cur is None or o.generated_at > cur.generated_at:
            last_per_code_day[key] = o
    dedup_ids = {id(o) for o in last_per_code_day.values()}

    # Bucket by (prompt_version, actionable)
    buckets: dict[tuple, dict[str, Any]] = {}
    for o in rows:
        key = (o.prompt_version or "?", o.actionable)
        b = buckets.setdefault(key, {
            "prompt_version": o.prompt_version or "?",
            "actionable": o.actionable,
            "n": 0, "hits": 0, "sum_return_d5": 0.0, "sum_excess_d5": 0.0,
            "n_unique": 0, "hits_unique": 0,
        })
        b["n"] += 1
        b["sum_return_d5"] += o.return_d5
        b["sum_excess_d5"] += o.return_d5 - seg_baseline[(_gen_day(o), market_board(o.code))]
        hit = _is_hit(o.actionable, o.return_d5)
        if hit:
            b["hits"] += 1
        if id(o) in dedup_ids:
            b["n_unique"] += 1
            if hit:
                b["hits_unique"] += 1

    summary = []
    for b in buckets.values():
        n = b["n"]
        nu = b["n_unique"]
        directional = b["actionable"] in ("建议买入", "建议卖出")
        summary.append({
            "prompt_version": b["prompt_version"],
            "actionable": b["actionable"],
            "n": n,
            "n_unique": nu,
            "hit_rate": round(b["hits"] / n * 100, 1) if (directional and n) else None,
            "hit_rate_dedup": round(b["hits_unique"] / nu * 100, 1) if (directional and nu) else None,
            "avg_return_d5": round(b["sum_return_d5"] / n, 2) if n else None,
            "excess_return_d5": round(b["sum_excess_d5"] / n, 2) if n else None,
        })
    summary.sort(key=lambda x: (x["prompt_version"], x["actionable"]))
    return {"total_scored": len(rows), "buckets": summary}


def hit_rate_by_model(since_days: int | None = None) -> dict[str, Any]:
    """Same scoring as hit_rate_stats but grouped by `model` instead of
    `prompt_version`. Built for the 6/20 A/B between minimax-m3 (default A)
    and kimi-k2.6 (B, 30%) — direct head-to-head readout.

    `since_days` filters to anchors generated in the last N days. Set this
    to the days since the A/B started so old data on the previous model
    doesn't pollute the average. None = all-time (legacy data included).
    """
    db: Session = SessionLocal()
    try:
        q = db.query(AnalysisOutcome).filter(AnalysisOutcome.return_d5.isnot(None))
        if since_days is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
            q = q.filter(AnalysisOutcome.generated_at >= cutoff)
        rows = q.all()
    finally:
        db.close()

    if not rows:
        return {"total_scored": 0, "buckets": [], "since_days": since_days}

    # Same-day, same-market baseline. Baseline is across all models (A/B 比
    # 模型要同 tape,baseline 得是公共地面),但**按 market 隔离** —— 不同
    # 板块波动量级不同(科创 ±20% vs 主板 ±10%),混在一条基线里会让科创票
    # 的 excess 把主板带偏。同 market 内 minimax vs kimi 仍共享基线、公平。
    from .stocks import market_board
    by_seg: dict[tuple, list[float]] = {}
    for o in rows:
        by_seg.setdefault((_gen_day(o), market_board(o.code)), []).append(o.return_d5)
    seg_baseline = {k: _median(vals) for k, vals in by_seg.items()}

    # Dedup: last anchor per (code, gen_day), regardless of model. A stock
    # that flipped from kimi to minimax intraday only counts under whichever
    # model produced its EOD verdict.
    last_per_code_day: dict[tuple, AnalysisOutcome] = {}
    for o in rows:
        key = (o.code, _gen_day(o))
        cur = last_per_code_day.get(key)
        if cur is None or o.generated_at > cur.generated_at:
            last_per_code_day[key] = o
    dedup_ids = {id(o) for o in last_per_code_day.values()}

    buckets: dict[tuple, dict[str, Any]] = {}
    for o in rows:
        key = (o.model or "?", o.actionable)
        b = buckets.setdefault(key, {
            "model": o.model or "?",
            "actionable": o.actionable,
            "n": 0, "hits": 0, "sum_return_d5": 0.0, "sum_excess_d5": 0.0,
            "n_unique": 0, "hits_unique": 0,
        })
        b["n"] += 1
        b["sum_return_d5"] += o.return_d5
        b["sum_excess_d5"] += o.return_d5 - seg_baseline[(_gen_day(o), market_board(o.code))]
        hit = _is_hit(o.actionable, o.return_d5)
        if hit:
            b["hits"] += 1
        if id(o) in dedup_ids:
            b["n_unique"] += 1
            if hit:
                b["hits_unique"] += 1

    summary = []
    for b in buckets.values():
        n = b["n"]
        nu = b["n_unique"]
        directional = b["actionable"] in ("建议买入", "建议卖出")
        summary.append({
            "model": b["model"],
            "actionable": b["actionable"],
            "n": n,
            "n_unique": nu,
            "hit_rate": round(b["hits"] / n * 100, 1) if (directional and n) else None,
            "hit_rate_dedup": round(b["hits_unique"] / nu * 100, 1) if (directional and nu) else None,
            "avg_return_d5": round(b["sum_return_d5"] / n, 2) if n else None,
            "excess_return_d5": round(b["sum_excess_d5"] / n, 2) if n else None,
        })
    summary.sort(key=lambda x: (x["model"], x["actionable"]))
    return {
        "total_scored": len(rows),
        "since_days": since_days,
        "buckets": summary,
    }


# 看平 band for nd_outlook_stats: |d1 return| within this % counts as a
# correct 看平 call. 1.0% ≈ a third of A-share daily典型振幅 — tight
# enough that 看平 isn't a free hit in any quiet tape.
# 看平命中判定带宽。6/16 从 1.0 → 2.5：A 股日均振幅 2-3%,±1% 带太窄,
# 次日涨跌几乎必然超出 → "看平"系统性判为没命中(6/16 实测看平 n=403
# 命中率仅 14.6%,但 avg_return_d1=+1.52% 说明方向并不离谱)。±2.5% 是
# "次日基本走平"的常识区间。纯统计口径,query-time 重算,不动预判逻辑。
ND_FLAT_BAND_PCT = 2.5


def nd_outlook_stats() -> dict[str, Any]:
    """Score next_day_outlook.trend against the actual next-day return.

    Scoring (vs d1 return):
      看涨 hit ⇔ ret > 0;  看跌 hit ⇔ ret < 0;
      看平 hit ⇔ |ret| ≤ ND_FLAT_BAND_PCT.

    Return basis: anchor_close → close_d1 when both available (dividend-
    safe, same qfq series); falls back to legacy return_d1 otherwise —
    `basis` counters expose the mix.

    Grouped two ways: by trend (is the directional claim worth anything?)
    and by nd_confidence (does its own 高/中/低 self-assessment stratify?).
    Anchors without nd_trend (pre-6/10) are excluded."""
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(AnalysisOutcome)
            .filter(
                AnalysisOutcome.nd_trend.isnot(None),
                AnalysisOutcome.close_d1.isnot(None),
            )
            .all()
        )
    finally:
        db.close()

    basis = {"anchor_close": 0, "anchor_price": 0}

    def _d1_ret(o: AnalysisOutcome) -> float | None:
        if o.anchor_close and o.close_d1 is not None:
            basis["anchor_close"] += 1
            return (o.close_d1 - o.anchor_close) / o.anchor_close * 100.0
        if o.return_d1 is not None:
            basis["anchor_price"] += 1
            return o.return_d1
        return None

    def _nd_hit(trend: str, ret: float) -> bool:
        if trend == "看涨":
            return ret > 0
        if trend == "看跌":
            return ret < 0
        if trend == "看平":
            return abs(ret) <= ND_FLAT_BAND_PCT
        return False

    by_trend: dict[str, dict[str, Any]] = {}
    by_conf: dict[str, dict[str, Any]] = {}
    scored = 0
    for o in rows:
        ret = _d1_ret(o)
        if ret is None:
            continue
        scored += 1
        hit = _nd_hit(o.nd_trend, ret)
        t = by_trend.setdefault(o.nd_trend, {"n": 0, "hits": 0, "sum": 0.0})
        t["n"] += 1
        t["sum"] += ret
        if hit:
            t["hits"] += 1
        c = by_conf.setdefault(o.nd_confidence or "?", {"n": 0, "hits": 0, "sum": 0.0})
        c["n"] += 1
        c["sum"] += ret
        if hit:
            c["hits"] += 1

    def _fmt(d: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for k, v in d.items():
            n = v["n"]
            out.append({
                "group": k,
                "n": n,
                "hit_rate": round(v["hits"] / n * 100, 1) if n else None,
                "avg_return_d1": round(v["sum"] / n, 2) if n else None,
            })
        out.sort(key=lambda x: -x["n"])
        return out

    return {
        "scored": scored,
        "flat_band_pct": ND_FLAT_BAND_PCT,
        "return_basis": basis,
        "by_trend": _fmt(by_trend),
        "by_nd_confidence": _fmt(by_conf),
    }


# Windows (in trading days) over which price predictions are scored.
# Two horizons so we see both the short fuse (d5, matches return_d5 / the
# nd scoring cadence) and the fuller swing-trade window (d20). Like
# nd_outlook_stats this is a query-time recompute over forward klines — no
# free-text valid_window parsing, no new stored columns.
PRICE_LEVEL_WINDOWS = [5, 20]


def price_level_stats() -> dict[str, Any]:
    """Score the LLM's price predictions (买入区/目标区/最紧止损) against
    forward klines. Mirror of nd_outlook_stats — pure埋点+打分, query-time.

    For each anchor that carries a buy_low (i.e. was recorded after the
    6/23 price埋点 went live) and has at least one forward kline, we walk
    the bars strictly after the anchor's generation day, in date order,
    within each window and measure:

      - touched_buy:  window min(low) ≤ buy_high  (did price reach the buy区?)
      - reached_target: window max(high) ≥ target_low  (did it hit目标?)
      - hit_stop:     window min(low) ≤ stop_price  (did it trip止损?)
      - target_first / stop_first / neither: scanning bars in order, which
        triggered first — high ≥ target_low or low ≤ stop_price. This is the
        only metric that needs ordered iteration; the other three are pure
        window extrema. Counted only when both target_low and stop_price
        are present on the anchor.

    Forward klines come from the same place backfill_outcomes reads them:
    klines for this code dated strictly after gen_day, ascending.

    Anchors recorded before the price埋点 (buy_low IS NULL) are excluded, so
    `scored` will sit near 0 until new anchors accumulate — that's expected.

    Returns {scored, windows: {d5: {...}, d20: {...}}}."""
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(AnalysisOutcome)
            .filter(AnalysisOutcome.buy_low.isnot(None))
            .all()
        )

        # Per-window accumulators.
        agg: dict[int, dict[str, int]] = {
            w: {
                "n": 0,
                "touched_buy": 0,
                "reached_target": 0,
                "hit_stop": 0,
                # target-vs-stop ordering (needs both levels present)
                "ordered_n": 0,
                "target_first": 0,
                "stop_first": 0,
                "neither": 0,
            }
            for w in PRICE_LEVEL_WINDOWS
        }
        scored = 0

        for o in rows:
            gen_day = _gen_day(o)
            future = (
                db.query(Kline)
                .filter(Kline.code == o.code, Kline.date > gen_day)
                .order_by(Kline.date.asc())
                .all()
            )
            if not future:
                continue
            scored += 1

            for w in PRICE_LEVEL_WINDOWS:
                bars = future[:w]
                if not bars:
                    continue
                lows = [b.low for b in bars if b.low is not None]
                highs = [b.high for b in bars if b.high is not None]
                a = agg[w]
                a["n"] += 1

                if o.buy_high is not None and lows and min(lows) <= o.buy_high:
                    a["touched_buy"] += 1
                if o.target_low is not None and highs and max(highs) >= o.target_low:
                    a["reached_target"] += 1
                if o.stop_price is not None and lows and min(lows) <= o.stop_price:
                    a["hit_stop"] += 1

                # Ordered target-vs-stop: only meaningful when both levels
                # exist. Walk bars in date order; first bar to satisfy either
                # condition decides. A bar that satisfies both in the same
                # day is scored as stop_first (conservative — the tighter
                # downside risk is assumed to have triggered intraday).
                if o.target_low is not None and o.stop_price is not None:
                    a["ordered_n"] += 1
                    outcome = "neither"
                    for b in bars:
                        hit_stop = b.low is not None and b.low <= o.stop_price
                        hit_tgt = b.high is not None and b.high >= o.target_low
                        if hit_stop:
                            outcome = "stop_first"
                            break
                        if hit_tgt:
                            outcome = "target_first"
                            break
                    a[outcome] += 1
    finally:
        db.close()

    def _pct(hits: int, n: int) -> float | None:
        return round(hits / n * 100, 1) if n else None

    windows: dict[str, dict[str, Any]] = {}
    for w in PRICE_LEVEL_WINDOWS:
        a = agg[w]
        n = a["n"]
        on = a["ordered_n"]
        windows[f"d{w}"] = {
            "n": n,
            "touched_buy": a["touched_buy"],
            "touched_buy_rate": _pct(a["touched_buy"], n),
            "reached_target": a["reached_target"],
            "reached_target_rate": _pct(a["reached_target"], n),
            "hit_stop": a["hit_stop"],
            "hit_stop_rate": _pct(a["hit_stop"], n),
            # target-vs-stop ordering
            "ordered_n": on,
            "target_first": a["target_first"],
            "stop_first": a["stop_first"],
            "neither": a["neither"],
            "target_first_rate": _pct(a["target_first"], on),
            "stop_first_rate": _pct(a["stop_first"], on),
        }

    return {"scored": scored, "windows": windows}


def hit_rate_by_confidence() -> dict[str, Any]:
    """Stratify hit_rate by confidence bucket across d1/d3/d5 horizons.

    Tests whether the LLM's self-reported confidence actually correlates
    with accuracy — i.e. whether 5/28's confidence-as-int system is
    doing real work or is just decoration.

    Buckets follow frontend's confidenceBucket():
      high: >= 80
      med:  60-79
      low:  < 60

    6/3 — returns d1/d3/d5 horizons in one shot. Confidence column on
    outcomes was added 5/29; the first d5-scored anchors with non-null
    confidence won't appear until ~6/5. d1/d3 light up earlier and give
    a preview of whether the field is meaningful. d5 stays the gold
    standard (hit_rate_stats uses d5 too).

    Expected pattern if confidence is meaningful:
      high.hit_rate > med.hit_rate > low.hit_rate
    Flat distribution = LLM throwing dice picking numbers.

    Only buy/sell anchors (directional). Anchors lacking confidence are
    excluded entirely (legacy).
    """
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(AnalysisOutcome)
            .filter(
                AnalysisOutcome.confidence.isnot(None),
                AnalysisOutcome.actionable.in_(["建议买入", "建议卖出"]),
            )
            .all()
        )
    finally:
        db.close()

    def bucket(c: int) -> str:
        if c >= 80:
            return "high"
        if c >= 60:
            return "med"
        return "low"

    def is_hit(actionable: str, ret: float) -> bool:
        if actionable == "建议买入":
            return ret > 0
        return ret < 0  # 建议卖出

    # Each bucket accumulates per-horizon counts.
    HORIZONS = ("d1", "d3", "d5")
    buckets: dict[tuple, dict[str, Any]] = {}
    for o in rows:
        key = (o.actionable, bucket(o.confidence))
        b = buckets.setdefault(key, {
            "actionable": o.actionable,
            "confidence_bucket": bucket(o.confidence),
            **{h: {"n": 0, "hits": 0, "sum": 0.0} for h in HORIZONS},
        })
        for h in HORIZONS:
            ret = getattr(o, f"return_{h}")
            if ret is None:
                continue
            b[h]["n"] += 1
            b[h]["sum"] += ret
            if is_hit(o.actionable, ret):
                b[h]["hits"] += 1

    summary = []
    for b in buckets.values():
        item = {
            "actionable": b["actionable"],
            "confidence_bucket": b["confidence_bucket"],
        }
        for h in HORIZONS:
            hb = b[h]
            n = hb["n"]
            item[h] = {
                "n": n,
                "hit_rate": round(hb["hits"] / n * 100, 1) if n else None,
                "avg_return": round(hb["sum"] / n, 2) if n else None,
            }
        summary.append(item)

    bucket_order = {"high": 0, "med": 1, "low": 2}
    summary.sort(key=lambda x: (x["actionable"], bucket_order[x["confidence_bucket"]]))

    # Roll-up totals per horizon: how many anchors of any bucket are
    # currently scored at that horizon. Useful for "do I have enough
    # sample to trust the comparison?"
    totals = {h: sum(b[h]["n"] for b in buckets.values()) for h in HORIZONS}

    return {
        "total_with_confidence": len(rows),  # row count irrespective of horizon
        "scored_per_horizon": totals,
        "buckets": summary,
    }
