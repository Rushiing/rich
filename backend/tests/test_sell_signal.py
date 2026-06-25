"""卖出线 S1 — sell_risk_signal 触发逻辑单测。

四类客观触发各自命中 + 健康票不触发。
跑法:cd backend && .venv/bin/python tests/test_sell_signal.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import AnalysisOutcome, Kline, PoolEntry, Snapshot
from app.services.sell_signal import sell_risk_signal


def _fresh():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _has(sig, key):
    return sig is not None and any(t["key"] == key for t in sig["triggers"])


def test_capital_outflow():
    db = _fresh()
    db.add(Snapshot(code="600000", price=10.0, main_net_flow=-1e6,
                    net_flow_3d=-3e6, change_pct=-0.5))
    db.commit()
    assert _has(sell_risk_signal(db, "600000"), "capital_outflow")
    print("✓ capital_outflow")


def test_below_ma20():
    db = _fresh()
    for d, c, m in [("2026-06-03", 9.0, 10.0), ("2026-06-02", 9.1, 10.0),
                    ("2026-06-01", 9.2, 10.0)]:
        db.add(Kline(code="600000", date=d, close=c, ma20=m))
    db.commit()
    assert _has(sell_risk_signal(db, "600000"), "below_ma20")
    # 只跌破 2 日不触发
    db2 = _fresh()
    for d, c, m in [("2026-06-03", 9.0, 10.0), ("2026-06-02", 9.1, 10.0),
                    ("2026-06-01", 10.5, 10.0)]:  # 最早一日在均线上
        db2.add(Kline(code="600000", date=d, close=c, ma20=m))
    db2.commit()
    assert not _has(sell_risk_signal(db2, "600000"), "below_ma20")
    print("✓ below_ma20 (含连3日边界)")


def test_thesis_invalidated():
    db = _fresh()
    db.add(Snapshot(code="600000", price=8.5, main_net_flow=1.0,
                    net_flow_3d=1.0, change_pct=0.0))
    db.add(PoolEntry(code="600000", source="rules", entry_close=10.0,
                     entry_date="2026-06-01", thesis={"invalidation_price": 9.0}))
    db.commit()
    assert _has(sell_risk_signal(db, "600000"), "thesis_invalidated")
    print("✓ thesis_invalidated")


def test_short_term_divergence():
    db = _fresh()
    db.add(Snapshot(code="600000", price=10.0, main_net_flow=1.0,
                    net_flow_3d=1.0, change_pct=-4.0))
    db.add(AnalysisOutcome(id=1, code="600000",
                           generated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                           actionable="建议买入", anchor_price=10.0, nd_trend="看涨"))
    db.commit()
    assert _has(sell_risk_signal(db, "600000"), "short_term_divergence")
    print("✓ short_term_divergence")


def test_healthy_no_signal():
    db = _fresh()
    db.add(Snapshot(code="600000", price=10.0, main_net_flow=1e6,
                    net_flow_3d=2e6, change_pct=1.2))
    for d, c, m in [("2026-06-03", 11.0, 10.0), ("2026-06-02", 10.9, 10.0),
                    ("2026-06-01", 10.8, 10.0)]:
        db.add(Kline(code="600000", date=d, close=c, ma20=m))
    db.commit()
    assert sell_risk_signal(db, "600000") is None
    print("✓ healthy → None")


if __name__ == "__main__":
    test_capital_outflow()
    test_below_ma20()
    test_thesis_invalidated()
    test_short_term_divergence()
    test_healthy_no_signal()
    print("\nALL PASS")
