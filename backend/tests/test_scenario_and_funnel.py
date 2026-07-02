"""③ 持仓情境埋点记分单测。

测两件:
- scenario_hit_stats():把解析的情境方向平移到买卖记分口径 —— 看多&涨=hit、
  看空&跌=hit、中性不计;只算 clean(returns_recomputed_at 非空)+ 有方向的行。
- funnel_situation_stats():用户漏斗选择的处境分布(held/盈亏/风险计数)。

跑法(无需 pytest):
    cd backend && .venv/bin/python tests/test_scenario_and_funnel.py
也兼容 pytest 收集(test_* 函数)。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import AnalysisOutcome, FunnelChoice
from app.services.outcomes import scenario_hit_stats, funnel_situation_stats


def _fresh_session():
    engine = create_engine("sqlite://")  # in-memory
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


_NOW = datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc)


def _outcome(_id, code, gen_day, ret, dirs, clean=True,
             prompt_version="v2.5-single"):
    # 7/2: scenario_hit_stats 只统计 -single 版本(与 by_actionable 同口径),
    # fixture 默认造 single 锚点;deep/debate 用 prompt_version 参数覆盖。
    return AnalysisOutcome(
        id=_id, code=code,
        generated_at=datetime(2026, 6, gen_day, 2, 0, tzinfo=timezone.utc),
        actionable="建议买入", anchor_price=10.0, anchor_close=10.0,
        return_d5=ret,
        returns_recomputed_at=_NOW if clean else None,
        scenario_directions=dirs,
        prompt_version=prompt_version,
    )


def test_scenario_hit_directions():
    """看多&涨=hit、看空&跌=hit、中性不计;unclean / 无方向行排除。"""
    db = _fresh_session()
    # row1: +5% (涨)
    db.add(_outcome(1, "600000", 1, 5.0, {
        "not_holding": "看多",       # 看多&涨 → hit
        "holding_big_gain": "看空",  # 看空&涨 → miss
        "holding_small": "中性",     # 中性 → 不计
        "holding_big_loss": "看多",  # 看多&涨 → hit
    }))
    # row2: -3% (跌),不同日 → 不与 row1 dedup 合并
    db.add(_outcome(2, "600000", 2, -3.0, {
        "not_holding": "看空",       # 看空&跌 → hit
        "holding_big_gain": "看空",  # 看空&跌 → hit
        "holding_small": "看多",     # 看多&跌 → miss
        "holding_big_loss": "中性",  # 中性 → 不计
    }))
    # row3: clean 但无方向 → 排除
    db.add(_outcome(3, "600000", 3, 2.0, None))
    # row4: 有方向但 unclean(returns_recomputed_at=None)→ 排除
    db.add(_outcome(4, "600000", 4, 2.0, {"not_holding": "看多"}, clean=False))
    # row5: deep 档锚点 → 排除(by_scenario 只吃 -single,与 by_actionable 同口径)
    db.add(_outcome(5, "600000", 5, 8.0, {"not_holding": "看多"},
                    prompt_version="v2.6-deep"))
    db.commit()

    res = scenario_hit_stats(db=db)
    assert res["total_clean_rows"] == 2, res["total_clean_rows"]
    by = {s["scenario"]: s for s in res["scenarios"]}

    assert by["not_holding"]["n_scored"] == 2 and by["not_holding"]["hit_rate"] == 100.0
    assert by["holding_big_gain"]["n_scored"] == 2 and by["holding_big_gain"]["hit_rate"] == 50.0
    assert by["holding_small"]["n_scored"] == 1 and by["holding_small"]["hit_rate"] == 0.0
    assert by["holding_small"]["n_neutral"] == 1
    assert by["holding_big_loss"]["n_scored"] == 1 and by["holding_big_loss"]["hit_rate"] == 100.0
    assert by["holding_big_loss"]["n_neutral"] == 1
    print("✓ test_scenario_hit_directions")


def test_scenario_empty_safe():
    """无数据时不崩,返回空结构。"""
    db = _fresh_session()
    res = scenario_hit_stats(db=db)
    assert res["total_clean_rows"] == 0
    assert all(s["hit_rate"] is None for s in res["scenarios"])
    print("✓ test_scenario_empty_safe")


def test_funnel_distribution():
    """处境分布 + held_rate + distinct 计数。"""
    db = _fresh_session()
    rows = [
        (1, 1, "600000", True, "盈", "aggressive"),
        (2, 1, "600001", True, "亏", "neutral"),
        (3, 2, "600000", False, None, "conservative"),
        (4, 2, "600002", True, "盈", "aggressive"),
    ]
    for _id, uid, code, held, pnl, tier in rows:
        db.add(FunnelChoice(
            id=_id, user_id=uid, code=code, held=held, pnl=pnl,
            tier=tier, anchor_close=10.0,
        ))
    db.commit()

    res = funnel_situation_stats(db=db)
    assert res["total_choices"] == 4
    assert res["distinct_users"] == 2
    assert res["distinct_codes"] == 3
    assert res["held_rate"] == 75.0
    assert res["by_situation"] == {"持有·盈": 2, "持有·亏": 1, "未持仓": 1}, res["by_situation"]
    assert res["by_pnl"] == {"盈": 2, "亏": 1}
    assert res["by_tier"] == {"aggressive": 2, "neutral": 1, "conservative": 1}
    print("✓ test_funnel_distribution")


if __name__ == "__main__":
    test_scenario_hit_directions()
    test_scenario_empty_safe()
    test_funnel_distribution()
    print("\nALL PASS")
