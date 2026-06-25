"""卖出线 S1 — sell_risk_signal 触发逻辑单测。

四类客观触发各自命中 + 健康票不触发 + 新鲜度/池状态护栏(codex S1 review P1)。
跑法:cd backend && .venv/bin/python tests/test_sell_signal.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import AnalysisOutcome, Kline, PoolEntry, Snapshot
from app.services.sell_signal import sell_risk_signal

AS_OF = datetime(2026, 6, 3, 6, 0, tzinfo=timezone.utc)


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _sig(db, code="600000"):
    return sell_risk_signal(db, code, as_of=AS_OF)


def _has(sig, key):
    return sig is not None and any(t["key"] == key for t in sig["triggers"])


def _ma_bars(db, code, closes):
    # closes: 由近到远 3 个收盘,ma20=10
    for i, c in enumerate(closes):
        d = (AS_OF.date() - timedelta(days=i)).isoformat()
        db.add(Kline(code=code, date=d, close=c, ma20=10.0))


def test_capital_outflow():
    db = _db()
    db.add(Snapshot(code="600000", ts=AS_OF, price=10.0, main_net_flow=-1e6,
                    net_flow_3d=-3e6, change_pct=-0.5))
    db.commit()
    assert _has(_sig(db), "capital_outflow")
    print("✓ capital_outflow")


def test_stale_snapshot_suppresses():
    """停牌/抓取断:10 天前的旧 snapshot 不该再触发资金/背离。"""
    db = _db()
    db.add(Snapshot(code="600000", ts=AS_OF - timedelta(days=10), price=10.0,
                    main_net_flow=-1e6, net_flow_3d=-3e6, change_pct=-5.0))
    db.commit()
    assert _sig(db) is None
    print("✓ 陈旧 snapshot 不触发")


def test_below_ma20():
    db = _db()
    _ma_bars(db, "600000", [9.0, 9.1, 9.2])  # 连 3 日 < ma20
    db.commit()
    assert _has(_sig(db), "below_ma20")
    db2 = _db()
    _ma_bars(db2, "600000", [9.0, 9.1, 10.5])  # 最早一日在均线上
    db2.commit()
    assert not _has(_sig(db2), "below_ma20")
    print("✓ below_ma20 (含连3日边界)")


def test_stale_klines_suppress_ma20():
    """退市/停牌:最近 K 线已是 30 天前,不该触发破位。"""
    db = _db()
    for i, c in enumerate([9.0, 9.1, 9.2]):
        d = (AS_OF.date() - timedelta(days=30 + i)).isoformat()
        db.add(Kline(code="600000", date=d, close=c, ma20=10.0))
    db.commit()
    assert not _has(_sig(db), "below_ma20")
    print("✓ 陈旧 K 线不触发破位")


def test_thesis_invalidated_active_only():
    db = _db()
    db.add(Snapshot(code="600000", ts=AS_OF, price=8.5, main_net_flow=1.0,
                    net_flow_3d=1.0, change_pct=0.0))
    db.add(PoolEntry(code="600000", source="rules", entry_close=10.0,
                     entry_date="2026-06-01", thesis={"invalidation_price": 9.0}))
    db.commit()
    assert _has(_sig(db), "thesis_invalidated")
    # 已淘汰的池行不该触发
    db2 = _db()
    db2.add(Snapshot(code="600000", ts=AS_OF, price=8.5, main_net_flow=1.0,
                     net_flow_3d=1.0, change_pct=0.0))
    db2.add(PoolEntry(code="600000", source="rules", state="eliminated",
                      entry_close=10.0, entry_date="2026-06-01",
                      thesis={"invalidation_price": 9.0}))
    db2.commit()
    assert not _has(_sig(db2), "thesis_invalidated")
    print("✓ thesis_invalidated (仅 active 池)")


def test_short_term_divergence():
    db = _db()
    db.add(Snapshot(code="600000", ts=AS_OF, price=10.0, main_net_flow=1.0,
                    net_flow_3d=1.0, change_pct=-4.0))
    db.add(AnalysisOutcome(id=1, code="600000", generated_at=AS_OF,
                           actionable="建议买入", anchor_price=10.0, nd_trend="看涨"))
    db.commit()
    assert _has(_sig(db), "short_term_divergence")
    # 旧分析(30 天前看涨)不该触发今日背离
    db2 = _db()
    db2.add(Snapshot(code="600000", ts=AS_OF, price=10.0, main_net_flow=1.0,
                     net_flow_3d=1.0, change_pct=-4.0))
    db2.add(AnalysisOutcome(id=1, code="600000", generated_at=AS_OF - timedelta(days=30),
                            actionable="建议买入", anchor_price=10.0, nd_trend="看涨"))
    db2.commit()
    assert not _has(_sig(db2), "short_term_divergence")
    print("✓ short_term_divergence (仅近期看涨)")


def test_healthy_no_signal():
    db = _db()
    db.add(Snapshot(code="600000", ts=AS_OF, price=10.0, main_net_flow=1e6,
                    net_flow_3d=2e6, change_pct=1.2))
    _ma_bars(db, "600000", [11.0, 10.9, 10.8])
    db.commit()
    assert _sig(db) is None
    print("✓ healthy → None")


if __name__ == "__main__":
    test_capital_outflow()
    test_stale_snapshot_suppresses()
    test_below_ma20()
    test_stale_klines_suppress_ma20()
    test_thesis_invalidated_active_only()
    test_short_term_divergence()
    test_healthy_no_signal()
    print("\nALL PASS")
