"""卖出线 S2 — 锚点 + 避免回撤记分单测。

验证:backfill 从 qfq Kline 算 return_d5;sell_signal_stats 用同日同板块**市场基线**
(AnalysisOutcome 当天全体 board 中位)算超额,avg_excess_d5<0 = 触发后跑输 = 有 edge。
跑法:cd backend && .venv/bin/python tests/test_sell_outcomes.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import AnalysisOutcome, Kline, SellSignalOutcome
from app.services.sell_outcomes import (
    backfill_sell_returns, record_sell_signal, sell_signal_stats,
)

FIRED = datetime(2026, 6, 1, 6, 0, tzinfo=timezone.utc)  # → BJT 06-01
NOW = datetime(2026, 6, 20, tzinfo=timezone.utc)


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _ao(db, _id, code, ret):
    """市场基线用:同日 clean 的分析行。"""
    db.add(AnalysisOutcome(
        id=_id, code=code, generated_at=FIRED, actionable="建议买入",
        anchor_price=10.0, anchor_close=10.0, return_d5=ret,
        returns_recomputed_at=NOW,
    ))


def test_backfill_and_avoid_drawdown():
    db = _db()
    # A 600000(主板):锚 06-01 收 10,d5(06-06)收 9 → return_d5 = -10%
    for d, c in [("2026-06-01", 10.0), ("2026-06-02", 9.8), ("2026-06-03", 9.6),
                 ("2026-06-04", 9.4), ("2026-06-05", 9.2), ("2026-06-06", 9.0)]:
        db.add(Kline(code="600000", date=d, close=c, ma20=10.0))
    # 市场基线:同日主板另两只 +2 / +4 → 中位 +3
    _ao(db, 1, "600001", 2.0)
    _ao(db, 2, "600003", 4.0)
    db.commit()

    record_sell_signal(db, "600000", {"level": 1, "triggers": [{"key": "capital_outflow"}]}, fired_at=FIRED)
    res = backfill_sell_returns(db=db)
    assert res["clean"] == 1 and res["no_basis"] == 0, res

    o = db.query(SellSignalOutcome).first()
    assert abs(o.anchor_close - 10.0) < 1e-9, o.anchor_close
    assert abs(o.return_d5 - (-10.0)) < 1e-6, o.return_d5

    stats = sell_signal_stats(db=db)
    assert stats["total_clean"] == 1
    # A 跌 10%,市场 +3 → 超额 -13 < 0 = 触发后跑输 = 卖出信号对
    assert abs(stats["overall"]["avg_excess_d5"] - (-13.0)) < 1e-6, stats["overall"]
    assert stats["overall"]["underperform_rate"] == 100.0
    assert "capital_outflow" in stats["by_trigger"]
    assert stats["by_trigger"]["capital_outflow"]["avg_excess_d5"] == -13.0
    print("✓ backfill + 避免回撤超额(跑输=hit)")


def test_outperformer_not_hit():
    """触发后反而跑赢市场 → 不算 hit(卖错了)。"""
    db = _db()
    for d, c in [("2026-06-01", 10.0), ("2026-06-02", 10.5), ("2026-06-03", 11.0),
                 ("2026-06-04", 11.5), ("2026-06-05", 12.0), ("2026-06-06", 13.0)]:
        db.add(Kline(code="600000", date=d, close=c, ma20=10.0))
    _ao(db, 1, "600001", 2.0)
    _ao(db, 2, "600003", 4.0)
    db.commit()
    record_sell_signal(db, "600000", {"level": 1, "triggers": [{"key": "below_ma20"}]}, fired_at=FIRED)
    backfill_sell_returns(db=db)
    stats = sell_signal_stats(db=db)
    # A +30%,市场 +3 → 超额 +27 > 0 → 不 hit
    assert stats["overall"]["avg_excess_d5"] > 0, stats["overall"]
    assert stats["overall"]["underperform_rate"] == 0.0
    print("✓ 跑赢市场 → 不 hit")


def test_empty_safe():
    db = _db()
    assert backfill_sell_returns(db=db) == {"scanned": 0, "clean": 0, "no_basis": 0}
    s = sell_signal_stats(db=db)
    assert s["total_clean"] == 0 and s["overall"]["avg_excess_d5"] is None
    print("✓ 空数据安全")


if __name__ == "__main__":
    test_backfill_and_avoid_drawdown()
    test_outperformer_not_hit()
    test_empty_safe()
    print("\nALL PASS")
