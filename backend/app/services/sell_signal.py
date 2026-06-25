"""卖出线 S1 — 当前状态风险信号引擎(纯客观,与买入解耦)。

遵 docs/three-line-principle.md + docs/sell-line-v1-plan.md。

不预测下跌、不看空 —— 只判「这票**当前可观测状态**是否转弱」,跟用户入场点
完全脱钩,跑在用户**全部自选**上(含 RICH 从没推荐过的存量票)。

触发(任一即风险,**permissive**:信号宽进,『是否真值得卖』由 S2 的避免回撤秤
事后判 —— 别在信号层就替秤下结论):
- capital_outflow  资金转流出:主力当日 + 近 3 日持续净流出
- below_ma20       技术破位:收盘 < MA20 连 3 个交易日
- thesis_invalidated 破失效线:该票在预选池且现价跌破 thesis.invalidation_price
- short_term_divergence 短线背离:最新分析判『看涨』但今日大幅下挫

返回 {level, triggers:[{key, reason}]},无风险 → None。每条 trigger 带人话 reason
(供 S3 解释,**不预测**)。S1 自身不下「该卖」结论、不碰买入分析、纯新增旁路。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AnalysisOutcome, Kline, PoolEntry, Snapshot


def sell_risk_signal(db: Session, code: str) -> dict | None:
    """判断 code 当前是否处于风险状态。纯客观、per-stock、全程 None-safe。"""
    triggers: list[dict[str, str]] = []

    snap = (
        db.query(Snapshot)
        .filter(Snapshot.code == code)
        .order_by(Snapshot.ts.desc())
        .first()
    )
    price = snap.price if snap else None

    # 1) 资金转流出:主力当日净流出 + 近 3 日累计净流出。
    if snap and snap.main_net_flow is not None and snap.net_flow_3d is not None:
        if snap.main_net_flow < 0 and snap.net_flow_3d < 0:
            triggers.append({
                "key": "capital_outflow",
                "reason": "主力资金当日 + 近 3 日持续净流出",
            })

    # 2) 技术破位:最近收盘 < MA20 连续 3 个交易日(ma20 已由 kline 预算)。
    bars = (
        db.query(Kline)
        .filter(Kline.code == code)
        .order_by(Kline.date.desc())
        .limit(3)
        .all()
    )
    if len(bars) == 3 and all(
        b.close is not None and b.ma20 is not None and b.close < b.ma20
        for b in bars
    ):
        triggers.append({
            "key": "below_ma20",
            "reason": "连续 3 日收盘跌破 20 日均线",
        })

    # 3) 破预选池失效线:该票在池中且现价跌破 thesis 的失效价(客观、机器可验)。
    if price is not None:
        pe = (
            db.query(PoolEntry)
            .filter(PoolEntry.code == code)
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

    # 4) 短线背离:最新分析判『看涨』,但今日大幅下挫(change_pct < -3%)。
    if snap and snap.change_pct is not None and snap.change_pct < -3:
        last_o = (
            db.query(AnalysisOutcome)
            .filter(AnalysisOutcome.code == code)
            .order_by(AnalysisOutcome.generated_at.desc())
            .first()
        )
        if last_o and last_o.nd_trend == "看涨":
            triggers.append({
                "key": "short_term_divergence",
                "reason": f"模型看涨,今日却跌 {snap.change_pct:.1f}%(短线背离)",
            })

    if not triggers:
        return None
    return {"level": len(triggers), "triggers": triggers}
