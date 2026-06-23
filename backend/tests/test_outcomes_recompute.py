"""Regression test for recompute_returns_from_close (codex 审计 P1/P3).

核心要测的是:recompute **不信任已落库的 close_dN / anchor_close**,而是对每行
从当前 Kline 表重读 anchor_bar + future,在同一读视图里重写三个字段。这样即使
旧 close_dN 来自另一套 qfq 快照,重算结果也只来自当前同一套表。

跑法(无需 pytest):
    cd backend && .venv/bin/python tests/test_outcomes_recompute.py
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
from app.models import AnalysisOutcome, Kline
from app.services.outcomes import recompute_returns_from_close


def _fresh_session():
    engine = create_engine("sqlite://")  # in-memory
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_klines(db, code: str):
    # 当前 qfq 表:锚点日 06-01 收 10.0,之后 5 个交易日到 06-06 收 11.0
    bars = [
        ("2026-06-01", 10.0),
        ("2026-06-02", 10.2),
        ("2026-06-03", 10.4),
        ("2026-06-04", 10.6),
        ("2026-06-05", 10.8),
        ("2026-06-06", 11.0),  # d5
    ]
    for d, c in bars:
        db.add(Kline(code=code, date=d, close=c))
    db.commit()


def test_recompute_uses_current_klines_not_stale_stored():
    """旧 close_d5 / anchor_close 来自另一批次(故意写脏),recompute 应忽略它们、
    用当前 Kline 重算。"""
    db = _fresh_session()
    code = "600000"
    _seed_klines(db, code)
    # 脏锚点:anchor_price 未复权(10.5)、anchor_close 旧批次(9.5)、close_d5 旧批次(10.8)
    db.add(AnalysisOutcome(
        id=1,
        code=code,
        generated_at=datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc),
        actionable="建议买入",
        anchor_price=10.5,
        anchor_close=9.5,     # stale
        close_d5=10.8,        # stale,来自另一套 qfq
        return_d5=2.857,      # 老 bug 值 (10.8-10.5)/10.5
    ))
    db.commit()

    res = recompute_returns_from_close(db=db)
    o = db.query(AnalysisOutcome).filter_by(code=code).first()

    # 三个字段都应来自当前 Kline:anchor_close=10.0、close_d5=11.0、return_d5=10.0
    assert abs(o.anchor_close - 10.0) < 1e-9, o.anchor_close
    assert abs(o.close_d5 - 11.0) < 1e-9, o.close_d5
    assert abs(o.return_d5 - 10.0) < 1e-9, o.return_d5
    assert res["clean"] == 1 and res["no_basis"] == 0
    assert res["changed"] == 1
    assert o.returns_recomputed_at is not None  # clean → 数据自证标记
    print("✓ 重读当前 Kline、忽略脏 close_dN/anchor_close + 盖 recomputed 标记")


def test_idempotent_second_run():
    """固定 Kline 状态下二次运行 changed=0。"""
    db = _fresh_session()
    code = "600000"
    _seed_klines(db, code)
    db.add(AnalysisOutcome(
        id=1,
        code=code, generated_at=datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc),
        actionable="建议买入", anchor_price=10.5,
    ))
    db.commit()
    recompute_returns_from_close(db=db)      # 第一遍
    res2 = recompute_returns_from_close(db=db)  # 第二遍
    assert res2["changed"] == 0, res2
    print("✓ 幂等:二次运行 changed=0")


def test_no_basis_when_kline_purged():
    """K 线已被滚动缓存淘汰(无 ≤gen_day 的 bar)→ 计 no_basis、不动该行。"""
    db = _fresh_session()
    code = "600000"
    # 只有 gen_day 之后的 K 线,没有 ≤gen_day 的锚点 bar(模拟老 outcome 被淘汰)
    db.add(Kline(code=code, date="2026-06-10", close=20.0))
    db.add(AnalysisOutcome(
        id=1,
        code=code, generated_at=datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc),
        actionable="建议买入", anchor_price=10.5, return_d5=3.0,
    ))
    db.commit()
    res = recompute_returns_from_close(db=db)
    o = db.query(AnalysisOutcome).filter_by(code=code).first()
    assert res["no_basis"] == 1 and res["clean"] == 0, res
    assert abs(o.return_d5 - 3.0) < 1e-9  # 留原值,不动
    print("✓ K线淘汰 → no_basis、原值不动")


def test_dividend_span_moves_return():
    """跨除权:用未复权 anchor_price 算 vs 当前 qfq anchor_close 算,收益显著不同。"""
    db = _fresh_session()
    code = "600000"
    _seed_klines(db, code)
    db.add(AnalysisOutcome(
        id=1,
        code=code, generated_at=datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc),
        actionable="建议买入",
        anchor_price=10.5,    # 未复权(除权前高)
        close_d5=11.0, return_d5=4.762,  # 老 bug:(11-10.5)/10.5
    ))
    db.commit()
    recompute_returns_from_close(db=db)
    o = db.query(AnalysisOutcome).filter_by(code=code).first()
    # 修后 (11-10)/10 = 10%,跟老的 4.76% 差 5.24pp
    assert abs(o.return_d5 - 10.0) < 1e-9
    print("✓ 跨除权:4.76% → 10.00%(基准修正)")


def test_future_short_of_d5_clears_d5_not_leave_stale():
    """future 不足 5 根:d3 重算;旧 d5 当前 K 线证明不了 → **清空**(不留旧口径
    残值混进 clean 统计)。codex P1 反例的回归。"""
    db = _fresh_session()
    code = "600000"
    # 锚点日 + 只有 3 个未来交易日(不够 d5)
    for d, c in [("2026-06-01", 10.0), ("2026-06-02", 10.2),
                 ("2026-06-03", 10.4), ("2026-06-04", 10.6)]:
        db.add(Kline(code=code, date=d, close=c))
    db.add(AnalysisOutcome(
        id=1, code=code, generated_at=datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc),
        actionable="建议买入", anchor_price=10.5,
        close_d5=10.9, return_d5=99.0,  # 旧口径残值,当前 K 线撑不到 d5
    ))
    db.commit()
    recompute_returns_from_close(db=db)
    o = db.query(AnalysisOutcome).filter_by(code=code).first()
    # d3 = 第 3 根 = 06-04(10.6),return_d3=6%
    assert o.close_d3 is not None and abs(o.return_d3 - 6.0) < 1e-9, o.return_d3
    # d5 撑不到 → 清空(关键:不是留 99.0)
    assert o.return_d5 is None and o.close_d5 is None, (o.return_d5, o.close_d5)
    # 行盖了 recomputed 标记,但因 return_d5=None,clean d5 统计(return_d5 非空
    # AND recomputed 非空)不会纳入它 —— 不变式成立
    assert o.returns_recomputed_at is not None
    print("✓ future 不足 d5:清空旧 d5(不混进 clean),d3 正常")


def test_nonpositive_anchor_close_goes_no_basis():
    """anchor_bar.close <= 0(脏 K 线):走 no_basis,anchor/returns 都不动(codex P2b)。"""
    db = _fresh_session()
    code = "600000"
    db.add(Kline(code=code, date="2026-06-01", close=0.0))   # 脏:收盘 0
    db.add(Kline(code=code, date="2026-06-06", close=11.0))
    db.add(AnalysisOutcome(
        id=1, code=code, generated_at=datetime(2026, 6, 1, 2, 0, tzinfo=timezone.utc),
        actionable="建议买入", anchor_price=10.5,
        anchor_close=9.9, return_d5=3.0,
    ))
    db.commit()
    res = recompute_returns_from_close(db=db)
    o = db.query(AnalysisOutcome).filter_by(code=code).first()
    assert res["no_basis"] == 1 and res["clean"] == 0, res
    assert abs(o.anchor_close - 9.9) < 1e-9 and abs(o.return_d5 - 3.0) < 1e-9  # 不动
    assert o.returns_recomputed_at is None  # 没清算 → 不该有标记
    print("✓ anchor_close<=0 → no_basis,原值不动、无 recomputed 标记")


if __name__ == "__main__":
    test_recompute_uses_current_klines_not_stale_stored()
    test_idempotent_second_run()
    test_no_basis_when_kline_purged()
    test_dividend_span_moves_return()
    test_future_short_of_d5_clears_d5_not_leave_stale()
    test_nonpositive_anchor_close_goes_no_basis()
    print("\nALL PASS")
