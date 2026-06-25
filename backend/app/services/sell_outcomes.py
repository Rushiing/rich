"""卖出线 S2 — 风险信号锚点 + 避免回撤记分(L1 秤 / claim 闸)。

**独立于买入 outcomes.py**(三线解耦:自己的表、记分、战绩,不借买入信用)。

口径(对称于买入 +5pp,但方向相反):
- 锚点:sell_risk_signal 触发即 record 一条 SellSignalOutcome。
- 回填:从当前 Kline(qfq)同批次取 fired 日 anchor_close + 前向 close_dN,算 return_dN,
  盖 returns_recomputed_at(复权安全、幂等)。
- 记分(避免回撤):clean 行里,信号触发后该票相对**同日同板块中位**的超额。
  **avg_excess_d5 < 0 = 触发后跑输板块 = 卖出信号有 edge**(避免了相对回撤)。
  去重保留每日每股最后一次触发;带 per-trigger 拆分。
- ⚠️ 初期 n 小,sell_signal_stats 标"样本不足、不对客"(claim 闸:60 天滚动跑赢才亮"有效")。
"""
from __future__ import annotations

import bisect
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import AnalysisOutcome, Kline, SellSignalOutcome
from .stocks import market_board

_BJT = timezone(timedelta(hours=8))


def _gen_day(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_BJT).date().isoformat()


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2


def record_sell_signal(
    db: Session, code: str, signal: dict, fired_at: datetime | None = None,
) -> None:
    """信号触发即 anchor 一条。anchor_close/return_dN 留给 backfill 从 qfq Kline 取。"""
    db.add(SellSignalOutcome(
        code=code,
        fired_at=fired_at or datetime.now(timezone.utc),
        triggers=[t["key"] for t in signal.get("triggers", [])] or None,
        level=signal.get("level", 1),
    ))
    db.commit()


_HORIZONS = {1: ("close_d1", "return_d1"), 5: ("close_d5", "return_d5"),
             20: ("close_d20", "return_d20")}


def backfill_sell_returns(db: Session | None = None) -> dict:
    """对每行从当前 Kline 同批次重算 anchor_close + close_dN + return_dN,盖 clean 戳。
    复权安全(anchor 与前向同一 qfq 序列)、幂等(固定 Kline 下可重跑)。"""
    own = db is None
    db = db or SessionLocal()
    try:
        rows = db.query(SellSignalOutcome).all()
        if not rows:
            return {"scanned": 0, "clean": 0, "no_basis": 0}
        codes = sorted({r.code for r in rows})
        bars_by_code: dict[str, list[tuple[str, float | None]]] = {}
        for kcode, kdate, kclose in (
            db.query(Kline.code, Kline.date, Kline.close)
            .filter(Kline.code.in_(codes))
            .order_by(Kline.code, Kline.date)
            .all()
        ):
            bars_by_code.setdefault(kcode, []).append((kdate, kclose))
        dates_by_code = {c: [d for d, _ in v] for c, v in bars_by_code.items()}

        now = datetime.now(timezone.utc)
        scanned = clean = no_basis = 0
        for r in rows:
            scanned += 1
            gen_day = _gen_day(r.fired_at)
            dates = dates_by_code.get(r.code)
            bars = bars_by_code.get(r.code)
            # anchor = fired 日(含)之前最后一根 K 线
            idx = bisect.bisect_right(dates, gen_day) - 1 if dates else -1
            anchor = bars[idx][1] if (bars and idx >= 0) else None
            if anchor is None or anchor <= 0:
                # K 线已淘汰、无可重读基准 → 清空 + 去 clean 戳
                r.anchor_close = None
                r.returns_recomputed_at = None
                for cattr, rattr in _HORIZONS.values():
                    setattr(r, cattr, None)
                    setattr(r, rattr, None)
                no_basis += 1
                continue
            r.anchor_close = anchor
            for h, (cattr, rattr) in _HORIZONS.items():
                fi = idx + h
                fclose = bars[fi][1] if fi < len(bars) else None
                if fclose is not None:
                    setattr(r, cattr, fclose)
                    setattr(r, rattr, (fclose - anchor) / anchor * 100)
                else:
                    setattr(r, cattr, None)
                    setattr(r, rattr, None)
            r.returns_recomputed_at = now
            clean += 1
        db.commit()
        return {"scanned": scanned, "clean": clean, "no_basis": no_basis}
    finally:
        if own:
            db.close()


def _bucket() -> dict[str, Any]:
    return {"n": 0, "hits": 0, "sum_ret": 0.0, "sum_excess": 0.0,
            "n_unique": 0, "hits_unique": 0}


def sell_signal_stats(db: Session | None = None) -> dict[str, Any]:
    """避免回撤记分(claim 闸)。只用 return_d5 + returns_recomputed_at 非空的 clean 行。
    hit = 触发后该票相对同日同板块**跑输**(excess_d5 < 0)= 卖出信号对。"""
    own = db is None
    db = db or SessionLocal()
    try:
        rows = (
            db.query(SellSignalOutcome)
            .filter(SellSignalOutcome.return_d5.isnot(None))
            .filter(SellSignalOutcome.returns_recomputed_at.isnot(None))
            .all()
        )
        # 市场参照基线:同日同板块,用 AnalysisOutcome 当天**被分析全体**的 board 中位
        # 当"市场那天" —— 不能用触发股自己(那样基线≈触发股中位,underperform_rate
        # 按构造 ≈50%、永远量不出 edge,codex P2)。这是中性市场事实,非借买入信用。
        needed_days = {_gen_day(o.fired_at) for o in rows}
        seg_baseline: dict[tuple, float] = {}
        if needed_days:
            tmp: dict[tuple, list[float]] = {}
            for gen_at, mcode, ret in (
                db.query(
                    AnalysisOutcome.generated_at,
                    AnalysisOutcome.code,
                    AnalysisOutcome.return_d5,
                )
                .filter(AnalysisOutcome.return_d5.isnot(None))
                .filter(AnalysisOutcome.returns_recomputed_at.isnot(None))
                .all()
            ):
                d = _gen_day(gen_at)
                if d in needed_days:
                    tmp.setdefault((d, market_board(mcode)), []).append(ret)
            seg_baseline = {k: _median(v) for k, v in tmp.items()}
    finally:
        if own:
            db.close()

    last: dict[tuple, SellSignalOutcome] = {}
    for o in rows:
        k = (o.code, _gen_day(o.fired_at))
        if k not in last or o.fired_at > last[k].fired_at:
            last[k] = o
    dedup_ids = {id(o) for o in last.values()}

    overall = _bucket()
    by_trigger: dict[str, dict[str, Any]] = {}
    baseline_missing = 0
    for o in rows:
        key = (_gen_day(o.fired_at), market_board(o.code))
        # 缺市场基线(当天无分析数据)→ **排除出统计**(codex 关门审 P1):若退化成
        # base=0,避免回撤秤就变成"绝对涨跌",口径污染结论。单列计数,不进 excess。
        if key not in seg_baseline:
            baseline_missing += 1
            continue
        base = seg_baseline[key]
        excess = o.return_d5 - base
        hit = excess < 0  # 触发后跑输板块 = 该卖
        is_dedup = id(o) in dedup_ids

        def _add(b: dict[str, Any]) -> None:
            b["n"] += 1
            b["sum_ret"] += o.return_d5
            b["sum_excess"] += excess
            if hit:
                b["hits"] += 1
            if is_dedup:
                b["n_unique"] += 1
                if hit:
                    b["hits_unique"] += 1

        _add(overall)
        for tk in (o.triggers or []):
            _add(by_trigger.setdefault(tk, _bucket()))

    def _fmt(b: dict[str, Any]) -> dict[str, Any]:
        n, nu = b["n"], b["n_unique"]
        return {
            "n": n,
            "n_unique": nu,
            # 触发后跑输同板块的比例(越高=信号越能识别该卖的)
            "underperform_rate": round(b["hits"] / n * 100, 1) if n else None,
            "underperform_rate_dedup": round(b["hits_unique"] / nu * 100, 1) if nu else None,
            # 头条:avg_excess_d5 < 0 = 有 edge(对称买入 +5pp)
            "avg_excess_d5": round(b["sum_excess"] / n, 2) if n else None,
            "avg_return_d5": round(b["sum_ret"] / n, 2) if n else None,
        }

    return {
        "total_clean": len(rows),
        "baseline_missing": baseline_missing,  # 缺市场基线、未进 excess 统计的行
        "overall": _fmt(overall),
        "by_trigger": {k: _fmt(v) for k, v in by_trigger.items()},
        "note": "avg_excess_d5 < 0 = 触发后跑输同板块 = 卖出信号有 edge(避免回撤);"
                "缺市场基线的行已排除(不退化成绝对涨跌);"
                "初期 n 小、样本不足、不对客(claim 闸:60 天滚动跑赢才亮『有效』)",
    }
