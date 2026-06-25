"""卖出线 S1 — 当前状态风险信号引擎(纯客观,与买入解耦)。

遵 docs/three-line-principle.md + docs/sell-line-v1-plan.md。

不预测下跌、不看空 —— 只判「这票**当前可观测状态**是否转弱」,跟用户入场点
完全脱钩,跑在用户**全部自选**上(含 RICH 从没推荐过的存量票)。

触发(任一即风险,**permissive**:信号宽进,『是否真值得卖』由 S2 的避免回撤秤
事后判 —— 别在信号层就替秤下结论):
- capital_outflow  资金转流出:主力当日 + 近 3 日持续净流出
- below_ma20       技术破位:收盘 < MA20 连 3 个交易日
- thesis_invalidated 破失效线:该票在**active 预选池**且现价跌破 thesis.invalidation_price
- short_term_divergence 短线背离:最新分析判『看涨』但今日大幅下挫

**新鲜度护栏(codex S1 review P1)**:停牌/抓取中断的旧 snapshot、几个月前的旧
分析、已淘汰的池子行,都不能再当"当前状态" —— 各触发按数据时效门控,过旧即不触发。
`as_of` 默认 now(),测试可注入固定时点。

返回 {level, triggers:[{key, reason}]},无风险 → None。S1 自身不下「该卖」结论、
不碰买入分析、纯新增旁路。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import AnalysisOutcome, Kline, PoolEntry, Snapshot

_ACTIVE_POOL_STATES = ("observing", "recommendable", "recommended")


def _fresh(ts: datetime | None, now: datetime, days: int) -> bool:
    """ts 是否在 now 往前 days 天内(None / 过旧 → False)。"""
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return timedelta(0) <= (now - ts) <= timedelta(days=days)


def _parse_kdate(s: str | None) -> date | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def sell_risk_signal(
    db: Session, code: str, as_of: datetime | None = None,
) -> dict | None:
    """判断 code 当前是否处于风险状态。纯客观、per-stock、全程 None-safe。"""
    now = as_of or datetime.now(timezone.utc)
    triggers: list[dict[str, str]] = []

    snap = (
        db.query(Snapshot)
        .filter(Snapshot.code == code)
        .order_by(Snapshot.ts.desc())
        .first()
    )
    # 停牌/抓取中断 → 旧 snapshot 不能再当"当前状态"。snapshot 类触发都需新鲜(≤2天)。
    snap_fresh = snap is not None and _fresh(snap.ts, now, 2)
    price = snap.price if (snap_fresh and snap.price and snap.price > 0) else None

    # 1) 资金转流出:主力当日净流出 + 近 3 日累计净流出(需新鲜 snapshot)。
    if snap_fresh and snap.main_net_flow is not None and snap.net_flow_3d is not None:
        if snap.main_net_flow < 0 and snap.net_flow_3d < 0:
            triggers.append({
                "key": "capital_outflow",
                "reason": "主力资金当日 + 近 3 日持续净流出",
            })

    # 2) 技术破位:最近收盘 < MA20 连续 3 个交易日,且最近 K 线新鲜(≤7 天,排除
    #    停牌/退市的旧数据继续触发)。ma20 已由 kline 预算。
    bars = (
        db.query(Kline)
        .filter(Kline.code == code)
        .order_by(Kline.date.desc())
        .limit(3)
        .all()
    )
    if len(bars) == 3:
        latest_kdate = _parse_kdate(bars[0].date)
        kline_fresh = latest_kdate is not None and (now.date() - latest_kdate).days <= 7
        if kline_fresh and all(
            b.close is not None and b.ma20 is not None and b.close < b.ma20
            for b in bars
        ):
            triggers.append({
                "key": "below_ma20",
                "reason": "连续 3 日收盘跌破 20 日均线",
            })

    # 3) 破预选池失效线:**仅 active 池**(已淘汰/历史重入行不算)+ 新鲜价。
    if price is not None:
        pe = (
            db.query(PoolEntry)
            .filter(
                PoolEntry.code == code,
                PoolEntry.state.in_(_ACTIVE_POOL_STATES),
            )
            .order_by(PoolEntry.id.desc())
            .first()
        )
        inv = (
            pe.thesis.get("invalidation_price")
            if (pe and isinstance(pe.thesis, dict))
            else None
        )
        if isinstance(inv, (int, float)) and inv > 0 and price < inv:
            triggers.append({
                "key": "thesis_invalidated",
                "reason": f"现价 {price:.2f} 跌破预选逻辑失效线 {inv:.2f}",
            })

    # 4) 短线背离:需新鲜 snapshot(今日大跌)+ 新鲜分析(≤7 天的看涨,避免几个月前
    #    的旧判断触发今日背离)。
    if snap_fresh and snap.change_pct is not None and snap.change_pct < -3:
        last_o = (
            db.query(AnalysisOutcome)
            .filter(AnalysisOutcome.code == code)
            .order_by(AnalysisOutcome.generated_at.desc())
            .first()
        )
        if last_o and last_o.nd_trend == "看涨" and _fresh(last_o.generated_at, now, 7):
            triggers.append({
                "key": "short_term_divergence",
                "reason": f"模型看涨,今日却跌 {snap.change_pct:.1f}%(短线背离)",
            })

    if not triggers:
        return None
    return {"level": len(triggers), "triggers": triggers}
