"""虚拟预选池 — B0/B1 (6/10).

The buy-side promise is 推荐有据可依: nothing reaches the user as a buy
recommendation until the system has watched it perform in a paper pool.
S0's honest stats showed why: buy verdicts have real selection alpha
(+8.5pp d5 excess) but terrible day-1 timing (29.3% hit — they fire after
strength and mean-revert). An observation period is exactly the filter
that kills the chase-the-spike failure mode.

Two entry channels (tagged via PoolEntry.source so a month of data can
rank them):
  - rules:        watchlist universe, breakout_20d + big_inflow signals
                  + latest profit_yoy > 0 + non-ST. Zero hallucination,
                  fully backtestable.
  - sector_picks: today's LLM sector picks (only when已生成 — the tick
                  never forces an LLM call).

Evaluation (daily post-close tick, kline-only price basis):
  - eliminate: close < invalidation_price (entry_close × 0.93), or
               ≥3 observed days and close < MA20. Rule text stored in
               thesis.invalidation_rule mirrors EXACTLY this code.
  - promote:   observing → recommendable when ≥5 observed trading days
               AND positive return AND close ≥ MA20 (when MA20 known).
  - eliminated rows keep their final metrics — failures are data.

Deliberate v1 narrowing: thesis is machine-verifiable price rules + the
evidence that triggered entry, NOT an LLM bull essay with free-text
catalysts — those would recreate the unverifiable-valid_window problem.
LLM thesis upgrade is deferred until this loop proves out.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Kline, PoolEntry, SectorPicks, Snapshot, Watchlist

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("observing", "recommendable")

# Hard invalidation: close below entry × (1 - 7%). 7% ≈ one bad A-share
# day past the typical 涨跌停-bounded noise band; tight enough to cut
# losers fast, loose enough to survive a normal pullback.
INVALIDATION_DROP_PCT = 7.0
# Soft invalidation: below MA20 after a grace period (day-1/2 wobble is
# noise; a 20-day-high breakout that can't hold MA20 by day 3 is dead).
MA20_GRACE_DAYS = 3
# Promotion gate: enough observation to dodge the d1 mean-reversion the
# outcomes data showed, plus the thesis still intact.
PROMOTE_MIN_DAYS = 5


def _invalidation_drop_pct(code: str) -> float:
    """硬失效线跌幅,按板块涨跌幅等比缩放(0.7×limit)。主板 10%→7.0
    (= INVALIDATION_DROP_PCT,零回归)、科创/创业 20%→14.0、北交 30%→21.0。
    -7% 对科创板 ±20% 波动太紧会被正常波动误杀,等比放大保持同一套风险容忍度。"""
    from .signals import _limit_pct
    return round(0.7 * _limit_pct(code), 1)

# 6/20: 指定板块通道 (designated). 内测用户反馈"多关注高科技 + 有色金属
# 板块的选股"。rules 通道只扫自选池、sector_picks 只覆盖当日 LLM TOP-5,
# 高科技/有色若当天没领涨就永远不进池 → 覆盖盲区。这条通道固定扫一组
# 优先板块,不靠当日排名。全局生效(不是 per-user)— 池子的核心价值是按
# 批次可对账的成绩单,per-user 会把样本碎成 1/10 毁掉对账,个性化交给
# 展示层(User.preferred_sectors 高亮)。
#
# sina 新浪行业只有 49 个粗板块,半导体/计算机/软件/通信 全揉进「电子信息」,
# 没有更细的分类(eastmoney 行业板块在 Railway 被墙)。质量闸够严,粗
# universe 也能筛出干净的票。theme 是入池 thesis.sector 标签 + per-user
# 偏好匹配的 canonical key。
PRIORITY_SECTORS = [
    {"theme": "科技·电子信息", "label": "new_dzxx"},  # 247 家:软件/IT/通信/半导体杂烩
    {"theme": "科技·电子器件", "label": "new_dzqj"},  # 152 家:元件/半导体硬件
    {"theme": "有色金属",      "label": "new_ysjs"},  # 72 家
]
# 每个 sina 板块按当日涨幅取 top N 进短名单,再 per-code 拉 K线/财报验闸。
# 471 只全拉扛不住;短名单把 per-code 调用压到 ~60 次/日。
DESIGNATED_SHORTLIST = 20


def _bjt_today() -> str:
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


def _is_st(name: str | None) -> bool:
    if not name:
        return False
    u = name.upper().replace(" ", "")
    return "ST" in u or "维权" in name


def _active_codes(db: Session) -> set[str]:
    rows = (
        db.query(PoolEntry.code)
        .filter(PoolEntry.state.in_(ACTIVE_STATES))
        .all()
    )
    return {r[0] for r in rows}


def active_codes(db: Session) -> set[str]:
    """Public 别名。6/18: 数据抓取 job(snapshot/quotes/financials/kline)
    用 watchlist ∪ active_codes 当 universe,让活跃池票(尤其 sector_picks
    通道、不在任何 watchlist 的)也有完整数据底座 — 否则晋升挂的解析因
    snapshot/财务/新闻缺失变空壳。前台 /stocks 仍按 watchlist 过滤,池子票
    数据存全局表但不进用户盯盘(区域不打架)。"""
    return _active_codes(db)


def _latest_kline(db: Session, code: str) -> Kline | None:
    return (
        db.query(Kline)
        .filter(Kline.code == code)
        .order_by(desc(Kline.date))
        .first()
    )


def _build_thesis(
    entry_close: float, evidence: list[str], sector: str | None = None,
    code: str = "",
) -> dict[str, Any]:
    drop = _invalidation_drop_pct(code) if code else INVALIDATION_DROP_PCT
    inv_price = round(entry_close * (1 - drop / 100), 2)
    t: dict[str, Any] = {
        "summary": " + ".join(evidence) if evidence else "",
        "evidence": evidence,
        "invalidation_price": inv_price,
        "invalidation_drop_pct": drop,  # 按板块缩放(科创 14 / 主板 7),淘汰文案读它
        "invalidation_rule": (
            f"收盘跌破 {inv_price}（入池价 -{drop:.0f}%）"
            f"，或入池 {MA20_GRACE_DAYS} 个交易日后收于 MA20 下方，即淘汰"
        ),
    }
    if sector:
        t["sector"] = sector
    return t


def _enter(
    db: Session, code: str, name: str | None, source: str,
    evidence: list[str], sector: str | None = None,
) -> bool:
    """Pull klines for the code (works for non-watchlist codes), anchor
    the entry on the latest qfq close, insert. Returns True on success."""
    from . import kline as kline_svc
    try:
        kline_svc.pull_one(code)
    except Exception as e:
        logger.warning("pool enter %s: kline pull failed (%s)", code, e)
    k = _latest_kline(db, code)
    if k is None or k.close is None:
        logger.warning("pool enter %s: no kline close available, skipping", code)
        return False
    db.add(PoolEntry(
        code=code,
        name=name,
        source=source,
        state="observing",
        entry_close=k.close,
        entry_date=k.date,
        thesis=_build_thesis(k.close, evidence, sector=sector, code=code),
        last_close=k.close,
        last_date=k.date,
        return_pct=0.0,
        max_drawdown_pct=0.0,
        days_observed=0,
    ))
    db.commit()
    logger.info("pool enter %s (%s) via %s @ %.2f", code, name, source, k.close)
    return True


# ---------------------------------------------------------------------------
# Entry channels
# ---------------------------------------------------------------------------

def scan_rules_channel(db: Session) -> dict[str, int]:
    """Watchlist universe: latest snapshot has breakout_20d AND big_inflow,
    latest financials show profit_yoy > 0, name is not ST, not already
    active in the pool。科创板未盈利硬科技免 profit 门槛(宽进)。"""
    from . import financials as fin_svc
    from .stocks import market_board

    active = _active_codes(db)
    names: dict[str, str] = {
        w.code: w.name for w in db.query(Watchlist).all()
    }
    entered = scanned = 0
    for code, name in names.items():
        if code in active or _is_st(name):
            continue
        snap = (
            db.query(Snapshot)
            .filter(Snapshot.code == code)
            .order_by(desc(Snapshot.id))
            .first()
        )
        if snap is None:
            continue
        sigs = set(snap.signals or [])
        scanned += 1
        if not ({"breakout_20d", "big_inflow"} <= sigs):
            continue
        fin_rows = fin_svc.latest_for_code(code, n=1)
        profit_yoy = fin_rows[0].profit_yoy if fin_rows else None
        is_star = market_board(code) == "star"
        # 宽进(科创板):未盈利硬科技放行,profit 门槛对它们不适用;突破+主力
        # 流入两闸照留。其余板块仍卡 profit_yoy>0。
        if not is_star and (profit_yoy is None or profit_yoy <= 0):
            continue
        flow_ev = (
            f"主力净流入 {snap.main_net_flow / 1e8:.1f} 亿"
            if snap.main_net_flow else "主力大额流入"
        )
        evidence = ["突破 20 日新高", flow_ev]
        if profit_yoy is not None and profit_yoy > 0:
            evidence.append(f"最新净利同比 {profit_yoy:+.0f}%")
        elif is_star:
            evidence.append("科创板·未盈利成长股")
        if _enter(db, code, name, "rules", evidence):
            entered += 1
    return {"scanned": scanned, "entered": entered}


def scan_sector_picks_channel(db: Session) -> dict[str, int]:
    """Today's cached sector picks (never forces an LLM call). Each pick
    carries its own reason — that becomes the evidence."""
    row = db.query(SectorPicks).filter(SectorPicks.id == 1).first()
    if row is None:
        return {"scanned": 0, "entered": 0, "skipped": "no_picks_row"}
    gen = row.generated_at
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    gen_bjt_day = gen.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    if gen_bjt_day != _bjt_today():
        return {"scanned": 0, "entered": 0, "skipped": "picks_not_today"}

    active = _active_codes(db)
    entered = scanned = 0
    for sec in (row.payload or {}).get("sectors", []):
        sec_name = sec.get("name")
        for p in sec.get("picks", []):
            code = p.get("code")
            if not code or code in active:
                continue
            scanned += 1
            name = p.get("name")
            if _is_st(name):
                continue
            evidence = [f"板块「{sec_name}」当日领涨" if sec_name else "板块精选"]
            if p.get("reason"):
                evidence.append(str(p["reason"])[:80])
            if _enter(db, code, name, "sector_picks", evidence, sector=sec_name):
                entered += 1
                active.add(code)
    return {"scanned": scanned, "entered": entered}


def scan_designated_sectors_channel(db: Session) -> dict[str, int]:
    """Fixed priority-sector scan — covers 高科技 + 有色金属 regardless of
    daily ranking. For each sina sector:
      1. one stock_sector_detail call → all constituents (cheap, has
         changepercent / amount / turnoverratio)
      2. shortlist: non-ST, trading (amount>0), top DESIGNATED_SHORTLIST
         by today's changepercent
      3. per-code gate (rules-equivalent, adapted for non-watchlist codes):
         - breakout_20d: close ≥ max(last-20 closes) — computed from kline
         - turnover relative strength: turnoverratio ≥ sector median
           (big_inflow substitute — main_net_flow needs a snapshot we don't
            have for non-watchlist constituents)
         - profit_yoy > 0: from financials (pulled per-code if missing)
    """
    import statistics
    from . import financials as fin_svc
    from . import kline as kline_svc
    from .stocks import market_board
    from .scraper import _safe_with_timeout
    import akshare as ak

    active = _active_codes(db)
    entered = scanned = 0
    for spec in PRIORITY_SECTORS:
        theme, label = spec["theme"], spec["label"]
        df = _safe_with_timeout(ak.stock_sector_detail, sector=label, _timeout=12.0)
        if df is None or len(df) == 0:
            logger.warning("designated: sector_detail empty for %s (%s)", theme, label)
            continue

        rows: list[tuple[str, str, float, float]] = []  # code, name, chg, turnover
        for _, r in df.iterrows():
            code = str(r.get("code") or "").strip()
            name = str(r.get("name") or "").strip()
            try:
                chg = float(r.get("changepercent") or 0)
                amount = float(r.get("amount") or 0)
                turnover = float(r.get("turnoverratio") or 0)
            except (ValueError, TypeError):
                continue
            if not code or _is_st(name) or amount <= 0:
                continue
            rows.append((code, name, chg, turnover))
        if not rows:
            continue

        median_turnover = statistics.median([r[3] for r in rows]) if rows else 0.0
        rows.sort(key=lambda x: x[2], reverse=True)  # by today's changepercent
        shortlist = rows[:DESIGNATED_SHORTLIST]

        for code, name, _chg, turnover in shortlist:
            if code in active:
                continue
            scanned += 1
            # gate 1: breakout_20d from kline
            try:
                kline_svc.pull_one(code)
            except Exception as e:
                logger.warning("designated %s: kline pull failed (%s)", code, e)
            recent = kline_svc.recent_for_code(code, days=20)
            closes = [k.close for k in recent if k.close is not None]
            if not closes or closes[-1] < max(closes) - 1e-9:
                continue  # not a fresh 20-day high
            # gate 2: turnover relative strength (big_inflow substitute)
            if turnover < median_turnover:
                continue
            # gate 3: profit_yoy > 0 — pull financials if we don't have them。
            # 宽进:科创板未盈利硬科技免此门槛(突破+换手两闸已过),其余仍卡。
            fin = fin_svc.latest_for_code(code, n=1)
            if not fin:
                try:
                    fin_svc.pull_for_code(code)
                    fin = fin_svc.latest_for_code(code, n=1)
                except Exception as e:
                    logger.warning("designated %s: financials pull failed (%s)", code, e)
            profit_yoy = fin[0].profit_yoy if fin else None
            is_star = market_board(code) == "star"
            if not is_star and (profit_yoy is None or profit_yoy <= 0):
                continue
            evidence = ["突破 20 日新高", f"板块「{theme}」内换手强度靠前"]
            if profit_yoy is not None and profit_yoy > 0:
                evidence.append(f"最新净利同比 {profit_yoy:+.0f}%")
            elif is_star:
                evidence.append("科创板·未盈利成长股")
            if _enter(db, code, name, "designated", evidence, sector=theme):
                entered += 1
                active.add(code)
    return {"scanned": scanned, "entered": entered}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _evaluate_entry(db: Session, e: PoolEntry) -> str:
    """Refresh metrics for one active entry and apply the state machine.
    Returns the (possibly new) state."""
    from . import kline as kline_svc
    try:
        kline_svc.pull_one(e.code)
    except Exception as ex:
        logger.warning("pool eval %s: kline pull failed (%s); using stored bars", e.code, ex)

    bars = (
        db.query(Kline)
        .filter(Kline.code == e.code, Kline.date > e.entry_date)
        .order_by(Kline.date.asc())
        .all()
    )
    now = datetime.now(timezone.utc)
    if not bars:
        e.updated_at = now
        db.commit()
        return e.state

    closes = [(b.date, b.close) for b in bars if b.close is not None]
    if not closes:
        e.updated_at = now
        db.commit()
        return e.state

    # 6/18 加固: 停牌告警。bars 里 close=None 的是停牌日,被 closes 过滤掉,
    # 所以 days_observed(=len(closes)) 在长期停牌时会停滞 → 票可能永远
    # 卡在 observing 不晋升不淘汰。log 出来便于发现僵尸票(阶段2 可加自动处理)。
    suspended = len(bars) - len(closes)
    if suspended >= 3:
        logger.warning(
            "pool eval %s: %d/%d 观察期 K 线无收盘(疑停牌),days_observed 停滞在 %d",
            e.code, suspended, len(bars), len(closes),
        )

    last_date, last_close = closes[-1]
    e.last_close = last_close
    e.last_date = last_date
    e.return_pct = (last_close - e.entry_close) / e.entry_close * 100.0
    e.days_observed = len(closes)
    # Max drawdown over the observation window, vs the running peak
    # (entry close included as the initial peak).
    peak = e.entry_close
    max_dd = 0.0
    for _, c in closes:
        peak = max(peak, c)
        dd = (c - peak) / peak * 100.0
        max_dd = min(max_dd, dd)
    e.max_drawdown_pct = max_dd
    e.updated_at = now

    latest = bars[-1]
    inv_price = (e.thesis or {}).get("invalidation_price")

    # Elimination — hard price floor, then MA20 after grace. 硬失效价(inv_price)
    # 入池时已按板块缩放存进 thesis,这里读存储值即自动市场化;文案的跌幅%
    # 同样读 thesis(老票没存则回退主板 7%)。
    drop_pct = float((e.thesis or {}).get("invalidation_drop_pct", INVALIDATION_DROP_PCT))
    if inv_price is not None and last_close < float(inv_price):
        e.state = "eliminated"
        e.state_changed_at = now
        e.eliminated_reason = (
            f"收盘 {last_close:.2f} 跌破失效线 {float(inv_price):.2f}"
            f"（入池价 -{drop_pct:.0f}%）"
        )
    elif (e.days_observed >= MA20_GRACE_DAYS
          and latest.ma20 is not None and last_close < latest.ma20):
        e.state = "eliminated"
        e.state_changed_at = now
        e.eliminated_reason = (
            f"入池 {e.days_observed} 个交易日后收于 MA20（{latest.ma20:.2f}）下方"
        )
    # Promotion — only from observing; recommendable rows just keep
    # updating metrics until elimination or (future) recommendation.
    elif (e.state == "observing"
          and e.days_observed >= PROMOTE_MIN_DAYS
          and (e.return_pct or 0) > 0
          # 6/18 加固: MA20 必须存在才晋升。原版 `ma20 is None or ...` 会在
          # MA20 缺失(新股/数据缺陷)时绕过技术面检验直接晋升 → 低质量推荐。
          and latest.ma20 is not None and last_close >= latest.ma20):
        e.state = "recommendable"
        e.state_changed_at = now
        # 6/18: 批次(cohort)— ISO 周,晋升那周归一期。
        bjt_now = now.astimezone(timezone(timedelta(hours=8)))
        e.cohort_week = bjt_now.strftime("%G-W%V")
        logger.info(
            "pool promote %s: %d days, %+.1f%%, above MA20, cohort=%s",
            e.code, e.days_observed, e.return_pct, e.cohort_week,
        )
        db.commit()  # 先落地晋升状态,再触发解析(解析失败不回滚晋升)
        # 6/18: 晋升即挂深度解析(single,不 debate)。sector_picks 票多不在
        # watchlist → allow_external + synthetic w;没 snapshot → anchor_price
        # 用 kline last_close。失败不阻塞(best-effort)。这是"全生命周期"+
        # 批次买卖命中率的数据源。
        try:
            from . import analysis as analysis_svc
            analysis_svc.generate(
                db, e.code, mode="single",
                allow_external=True, external_name=e.name,
                cohort=e.cohort_week,
                anchor_price_override=e.last_close,
            )
            logger.info("pool promote %s: analysis generated", e.code)
        except Exception:
            logger.exception("pool promote %s: analysis failed (non-fatal)", e.code)
        return e.state

    db.commit()
    return e.state


def evaluate_all(db: Session) -> dict[str, int]:
    entries = (
        db.query(PoolEntry)
        .filter(PoolEntry.state.in_(ACTIVE_STATES))
        .all()
    )
    counts = {"evaluated": 0, "eliminated": 0, "promoted": 0}
    for e in entries:
        before = e.state
        after = _evaluate_entry(db, e)
        counts["evaluated"] += 1
        if after == "eliminated":
            counts["eliminated"] += 1
        elif before == "observing" and after == "recommendable":
            counts["promoted"] += 1
    return counts


def run_pool_tick() -> dict[str, Any]:
    """Daily post-close tick: evaluate existing entries FIRST (yesterday's
    members judged on today's close before today's candidates dilute the
    pool), then scan both entry channels."""
    db: Session = SessionLocal()
    try:
        result: dict[str, Any] = {"ts": datetime.now(timezone.utc).isoformat()}
        result["evaluate"] = evaluate_all(db)
        result["rules"] = scan_rules_channel(db)
        result["sector_picks"] = scan_sector_picks_channel(db)
        result["designated"] = scan_designated_sectors_channel(db)
        logger.info("pool tick: %s", result)
        return result
    finally:
        db.close()


def pool_overview(db: Session, eliminated_limit: int = 15) -> dict[str, Any]:
    """Serialized pool state for the /api/pool route + diag."""
    def _row(e: PoolEntry) -> dict[str, Any]:
        return {
            "id": e.id,
            "code": e.code,
            "name": e.name,
            "source": e.source,
            "state": e.state,
            "entered_at": e.entered_at.isoformat() if e.entered_at else None,
            "entry_date": e.entry_date,
            "entry_close": e.entry_close,
            "last_close": e.last_close,
            "last_date": e.last_date,
            "return_pct": round(e.return_pct, 2) if e.return_pct is not None else None,
            "max_drawdown_pct": round(e.max_drawdown_pct, 2) if e.max_drawdown_pct is not None else None,
            "days_observed": e.days_observed,
            "thesis": e.thesis,
            "eliminated_reason": e.eliminated_reason,
            "cohort_week": e.cohort_week,
            "state_changed_at": e.state_changed_at.isoformat() if e.state_changed_at else None,
        }

    active = (
        db.query(PoolEntry)
        .filter(PoolEntry.state.in_(ACTIVE_STATES))
        .order_by(desc(PoolEntry.entered_at))
        .all()
    )
    eliminated = (
        db.query(PoolEntry)
        .filter(PoolEntry.state == "eliminated")
        .order_by(desc(PoolEntry.state_changed_at))
        .limit(eliminated_limit)
        .all()
    )
    return {
        "recommendable": [_row(e) for e in active if e.state == "recommendable"],
        "observing": [_row(e) for e in active if e.state == "observing"],
        "eliminated_recent": [_row(e) for e in eliminated],
        "counts": {
            "observing": sum(1 for e in active if e.state == "observing"),
            "recommendable": sum(1 for e in active if e.state == "recommendable"),
            "eliminated_total": db.query(PoolEntry).filter(PoolEntry.state == "eliminated").count(),
        },
    }
