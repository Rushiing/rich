"""LLM-driven deep analysis pipeline.

Pipeline:
  1. Load latest Snapshot for the code (or pull a fresh one if none exists).
  2. Render a prompt that mixes snapshot data + strategy rules.
  3. Call Claude with a single tool (`submit_analysis`) whose input schema
     enforces the key table shape AND a markdown deep_analysis field.
  4. Persist into the Analysis cache table (one row per code, replaced).

Caching: callers check the table's `created_at` against TTL. We don't TTL
inside this module — the route does it explicitly.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from anthropic import Anthropic
from sqlalchemy.orm import Session
from sqlalchemy import desc

from ..config import settings
from ..db import SessionLocal
from ..models import Analysis, Snapshot, Watchlist
from .strategy import Strategy, get as get_strategy

logger = logging.getLogger(__name__)

# Default analysis model. Overridable via ANALYSIS_MODEL env var without a
# code change — useful when the upstream gateway shifts models or when we
# want to A/B between providers (e.g. Sonnet vs kimi-k2.5).
#
# Why kimi-k2.5 (4/28): the Sonnet quota was exhausted; we benchmarked the
# 5 viable dashscope models on 300638 (a hard case) and kimi was the best
# trade-off — fastest reliable (~25s), most concise output, tied for most
# thorough red_flags, and the only fast one that supports the *forced*
# `tool_choice={"type":"tool", "name": ...}` shape so analysis.py needs
# no protocol change. Tone matches the user's "克制研究员" preference.
DEFAULT_MODEL = "kimi-k2.5"

# Prompt-version prefix. Bump when the tool schema or system prompt
# changes in a way that affects output content. We append the runtime
# `mode` to produce the actual stored value via prompt_version_for() —
# single and debate are categorically different output paths and need
# separate hit-rate buckets to A/B against each other.
#
# Historical note: until 5/28 this was hardcoded to "v2.5-debate"
# regardless of mode (bug), so all 1146 prod outcome rows are tagged
# "v2.5-debate" even though half were single-mode. Fix it on new
# anchors via prompt_version_for(); old rows can be retroactively
# corrected with a one-off UPDATE keyed on `mode` if needed.
PROMPT_VERSION_BASE = "v2.5"


def prompt_version_for(mode: str | None) -> str:
    """Stable prompt-version ID per (base, mode). Used by both the
    Analysis row tag and the outcome anchor — they must agree so
    hit_rate_stats can group consistently."""
    return f"{PROMPT_VERSION_BASE}-{mode or 'single'}"


# Back-compat shim: some external imports may still reach for PROMPT_VERSION.
# Defaults to the single-mode tag (the path that runs 99% of the time).
PROMPT_VERSION = prompt_version_for("single")


# ---------------------------------------------------------------------------
# Data completeness + market context
#
# Two pieces of upstream context fed to every analysis so the LLM can
# self-calibrate its confidence:
#
#   1. data_completeness — does the snapshot have what it needs? Four
#      dynamic dimensions, equally weighted (25 pts each). industry_name
#      is intentionally excluded — it's setup-level metadata (admin runs
#      /refresh-industry-meta once), not "today's input quality".
#
#   2. market context — 大盘 + 所属板块的今日表现. Pulled from existing
#      market_svc / sectors services (60s + 5min cache, no extra fetch
#      pressure). 板块匹配走 strict name compare; not matched → silent
#      skip that line (fail-safe so a missing sector entry doesn't bork
#      the whole analysis).
#
# Both are also persisted (data_completeness as a column on Analysis) so
# we can later correlate them with hit-rate.
# ---------------------------------------------------------------------------

# Max age (days) for the latest financial report's *period-end date*
# before we tag it "stale".
#
# 6/10: bumped 60 → 135. The age is measured against the report period end
# (e.g. Q1 = 03-31), not the disclosure date — and A-share disclosure
# deadlines mean the freshest available report is routinely 60-130 days
# past its period end (Q1 disclosed by 4/30, 半年报 by 8/31, so from June
# to late August the best you can have is the 3/31 report). At 60 the
# whole watchlist was "stale" for most of every quarter. 135 keeps the
# common between-filings windows fresh while still flagging the genuinely
# old cases (e.g. a company still showing Q3 data in late spring).
_FINANCIAL_STALE_DAYS = 135


def _is_intraday() -> bool:
    """Is right now within A-share market hours (BJT 9:30–11:30 + 13:00–15:00,
    Mon–Fri)?

    Used by compute_data_completeness() to be lenient on fund-flow fields
    that the data source only publishes post-close — punishing them
    during trading hours produced spurious "low confidence" verdicts on
    every batch run from open to ~15:30 BJT.

    Not exhaustive — doesn't account for holiday calendar. The
    consequence of a false positive (treating a holiday as trading) is
    just that fund-flow gaps get a free pass for that day; mild. The
    consequence of a false negative (rare; would need TZ skew) is the
    same as before this function existed. So accuracy beyond
    weekday+hour isn't worth the dependency.
    """
    now_bjt = datetime.now(timezone(timedelta(hours=8)))
    if now_bjt.weekday() >= 5:  # Sat/Sun
        return False
    hm = now_bjt.hour * 100 + now_bjt.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def compute_data_completeness(s: Snapshot | None, code: str) -> dict[str, Any]:
    """Score the input quality for this analysis run.

    Returns {score: int 0-100, missing: list[str], stale: list[str],
             intraday_skipped: list[str]}.

    Up to four equally-weighted dimensions:
      - 实时行情:  Snapshot.{price, change_pct, volume, turnover}
      - 技术指标:  Kline.latest_for_code(code) present + MA5/10/20/60 all non-null
      - 资金流向:  Snapshot.{main_net_flow, net_flow_3d, north_hold_change}
      - 财务数据:  Financial.latest_for_code(code, n=1) present + report ≤60d old

    **Intraday leniency (added 5/28 after open-to-15:00 batch runs all
    came back "低 置信"):** during A-share trading hours, the fund-flow
    dimension is regularly missing because the data source publishes it
    post-close. So when fund_missing AND _is_intraday(), we *drop the
    dimension entirely from the denominator* (not just zero its score)
    and record the fact in `intraday_skipped` for prompt transparency.
    Post-close, missing fund flow goes back into `missing` and costs
    25 pts as before.

    The result is that a fully-filled intraday snapshot scores 100/100
    (3 dims × 33.33 each) rather than 75/100, and the prompt section
    can honestly say "盘中暂无 (收盘后才齐), 不影响判断" instead of
    falsely flagging quality.

    If snapshot is None we score 0 with everything missing — caller is
    free to skip the prompt section entirely (the existing fallback path
    in _user_prompt already tells the LLM to drop confidence).
    """
    missing: list[str] = []
    stale: list[str] = []
    intraday_skipped: list[str] = []
    got = 0
    max_score = 0

    if s is None:
        return {
            "score": 0,
            "missing": ["snapshot"],
            "stale": [],
            "intraday_skipped": [],
            "info_missing": [],
            "peer_count": None,
        }

    is_intraday = _is_intraday()

    # 1. 实时行情 (25 pts, always in denominator)
    rt_fields = [("price", s.price), ("change_pct", s.change_pct),
                 ("volume", s.volume), ("turnover", s.turnover)]
    rt_missing = [name for name, v in rt_fields if v is None]
    max_score += 25
    if not rt_missing:
        got += 25
    else:
        missing.append(f"实时行情:{','.join(rt_missing)}")

    # 2. 技术指标 (25 pts, always in denominator) — late import to dodge any circular risk
    from . import kline as kline_svc
    latest_k = kline_svc.latest_for_code(code)
    max_score += 25
    if latest_k is None:
        missing.append("技术指标:K线未拉到")
    else:
        ma_fields = [("MA5", latest_k.ma5), ("MA10", latest_k.ma10),
                     ("MA20", latest_k.ma20), ("MA60", latest_k.ma60)]
        ma_missing = [name for name, v in ma_fields if v is None]
        if not ma_missing:
            got += 25
        else:
            missing.append(f"技术指标:{','.join(ma_missing)}")

    # 3. 资金流向 — intraday-lenient.
    fund_fields = [("main_net_flow", s.main_net_flow),
                   ("net_flow_3d", s.net_flow_3d),
                   ("north_hold_change", s.north_hold_change)]
    fund_missing = [name for name, v in fund_fields if v is None]
    if not fund_missing:
        # Got the data → counts toward both numerator and denominator.
        max_score += 25
        got += 25
    elif is_intraday:
        # Intraday + missing → drop the dimension. Record for prompt
        # transparency so the LLM can mention it but not get punished.
        intraday_skipped.append(f"资金流向({','.join(fund_missing)})")
    else:
        # Post-close + still missing → actually low quality.
        max_score += 25
        missing.append(f"资金流向:{','.join(fund_missing)}")

    # 4. 财务数据 (25 pts, always in denominator) — has row + ≤ _FINANCIAL_STALE_DAYS old
    from . import financials as fin_svc
    fin_rows = fin_svc.latest_for_code(code, n=1)
    max_score += 25
    if not fin_rows:
        missing.append("财务数据:未拉到")
    else:
        report_date = fin_rows[0].report_date
        # Financial.report_date is a String(10) "YYYYMMDD" (see models.py) —
        # NOT a date object. The pre-6/10 code did `today - report_date`
        # directly, which raised on every call and fell into the except
        # branch: every analysis was tagged "报告日期解析失败" and lost the
        # full 25-pt financial dimension, systematically depressing
        # data_completeness (and thus LLM confidence) across the board.
        try:
            if isinstance(report_date, str):
                rd = datetime.strptime(report_date.strip(), "%Y%m%d").date()
            elif hasattr(report_date, "date"):
                rd = report_date.date()
            else:
                rd = report_date
            today = datetime.now(timezone.utc).date()
            age_days = (today - rd).days
            if age_days <= _FINANCIAL_STALE_DAYS:
                got += 25
            else:
                stale.append(f"财务数据:最近季报截止 {age_days} 天前")
        except Exception:
            # Defensive: bad date format shouldn't crash the analysis
            stale.append("财务数据:报告日期解析失败")

    # Score = filled / available × 100. Intraday with fund_missing →
    # max_score is 75; otherwise 100.
    score = int(round(got / max_score * 100)) if max_score > 0 else 0

    # 6/9: info_missing — 新增 informational 类别 (不算分,只提示)。
    # 用来标注 shareholder / peer 数据缺失情况,LLM 能知道某些段为啥短。
    # 不进 score 是因为这些是"可选信号",有的话 confidence 可以更高,
    # 没有的话不算"输入质量差" — 跟 missing/stale 不同语义。
    info_missing: list[str] = []
    # peer_count exposed in the result (None = unknown/no industry) so the
    # prompt builder can suppress the self-referential 行业均值/分位 lines
    # when the industry pool is too thin to mean anything.
    peer_count: int | None = None
    if s is not None:
        # 同业可比: 同行业 < 3 个 stock 在 snapshot 池里就算 "可比股不足"
        try:
            if s.industry_name:
                from sqlalchemy import func as sql_func
                db = SessionLocal()
                try:
                    peer_count = (
                        db.query(sql_func.count(Snapshot.code.distinct()))
                        .filter(
                            Snapshot.industry_name == s.industry_name,
                            Snapshot.code != s.code,
                            Snapshot.pe_ratio.isnot(None),
                        ).scalar() or 0
                    )
                finally:
                    db.close()
                if peer_count < 3:
                    info_missing.append(f"同业可比股不足(行业「{s.industry_name}」只有 {peer_count} 支)")
            else:
                info_missing.append("行业未知,无同业可比")
        except Exception:
            pass
        # 股东变动: 90 天内无任何事件 (跟 latest_for_code 一致)
        try:
            from . import shareholder as shareholder_svc
            events = shareholder_svc.latest_for_code(code, days=90, n=1)
            if not events:
                info_missing.append("近 90 天无内部人交易事件")
        except Exception:
            pass

    return {
        "score": score,
        "missing": missing,
        "stale": stale,
        "intraday_skipped": intraday_skipped,
        "info_missing": info_missing,
        "peer_count": peer_count,
    }


def _shareholder_changes_section(code: str | None) -> str:
    """近 90 天董监高/高管/配偶子女增减持事件 section. Fail-safe: 任何
    异常 (DB error / table 不存在等) 直接返回 '' 不阻塞 prompt 构建。
    无事件时返回特殊文案 — 让 LLM 知道我们查过且无内幕活动,而不是
    数据缺失。
    """
    if not code:
        return ""
    try:
        from . import shareholder as shareholder_svc
        events = shareholder_svc.latest_for_code(code, days=90, n=15)
    except Exception as e:
        logger.warning("shareholder section: failed for %s (%s); skipping", code, e)
        return ""

    if not events:
        return (
            "\n## 内部人交易 (近 90 天)\n"
            "近 90 天内未查到董监高/高管/配偶子女增减持记录。"
            "(无活动属正常,绝大多数股票多数时间无内部人交易)\n"
        )

    # 简单汇总: 增持 vs 减持事件数 + 金额. 用 change_reason / change_shares 判方向
    # 东财 change_shares 正值通常代表增持,负值代表减持。一些"竞价交易"是双向的,
    # 用 holdings_after - opening 不容易,简化按 sign 分类。
    buys = [e for e in events if (e.change_shares or 0) > 0]
    sells = [e for e in events if (e.change_shares or 0) < 0]

    lines = ["\n## 内部人交易 (近 90 天,董监高/高管/配偶子女增减持)"]
    lines.append(
        f"汇总: 共 {len(events)} 次变动,其中增持 {len(buys)} 次 / 减持 {len(sells)} 次。"
    )

    # 详细列表 — 最多 8 条,按时间倒序 (events 已是 DESC)
    lines.append("\n详细列表 (最近 8 笔):")
    for e in events[:8]:
        direction = "增" if (e.change_shares or 0) > 0 else "减" if (e.change_shares or 0) < 0 else "?"
        shares_w = (abs(e.change_shares) / 1e4) if e.change_shares else 0
        amt_w = (abs(e.change_amount) / 1e4) if e.change_amount else 0
        # 例: "2025-12-15 [减] 张三 (高管/本人,因配偶子女): -12.5万股 @ 28.35 ≈ 354万元 · 竞价交易"
        line = (
            f"- {e.change_date} [{direction}] {e.person}"
            f" ({e.role or '?'}/{e.relation or '?'}):"
            f" {shares_w:.1f}万股"
        )
        if e.avg_price:
            line += f" @ {e.avg_price:.2f}"
        if amt_w > 0:
            line += f" ≈ {amt_w:.0f}万元"
        if e.change_reason:
            line += f" · {e.change_reason}"
        lines.append(line)

    lines.append(
        "\n判读提示:大股东/控股股东集中减持是负面信号;独立高管小额增持是中性偏正;"
        "大宗交易/协议转让可能是套现/接盘,看交易对手。"
    )
    return "\n".join(lines) + "\n"


def _peer_comparison_section(s: "Snapshot | None") -> str:
    """同行业 PE 最接近本股的 5 支可比股,横向对比 PE/PB/今日涨跌。

    Fail-safe: 无 industry_name / 同行业 < 3 支 / DB error → 返回 ''
    不阻塞。

    6/9 (A+B 调整):
    - B. 选取算法从"市值降序"改成"PE 接近本股"。原因:6/9 render-prompt
      验证发现按市值降序选出来 5 支 PE 跨度极大 (36 到 202),LLM 觉得
      列表代表性差,只用聚合的 industry_pe_avg 不引具体股票。按 PE 接近
      取 5 支,LLM 看到的是"真的可比的票",更愿意引用。
    - A. prompt 强化为"必须在 deep_analysis 看多/看空理由里至少引用
      1-2 支具体股票代码做对比",从被动判读变主动要求。
    """
    if s is None or s.pe_ratio is None:
        # 没有本股 PE 就没法算 PE 接近,直接不展示
        return ""
    try:
        from sqlalchemy import func
        from ..models import Snapshot
        db = SessionLocal()
        try:
            self_pe = s.pe_ratio
            same_industry_peers: list = []
            # 第一阶段:严格 industry_name 匹配
            if s.industry_name:
                subq = (
                    db.query(Snapshot.code, func.max(Snapshot.id).label("max_id"))
                    .filter(
                        Snapshot.industry_name == s.industry_name,
                        Snapshot.code != s.code,
                        Snapshot.pe_ratio.isnot(None),
                    )
                    .group_by(Snapshot.code).subquery()
                )
                same_industry_peers = (
                    db.query(Snapshot)
                    .join(subq, Snapshot.id == subq.c.max_id)
                    .order_by(func.abs(Snapshot.pe_ratio - self_pe).asc())
                    .limit(5).all()
                )

            # 第二阶段 fallback: 同行业 < 3 支时,跨行业按 PE 接近 + 市值同
            # 量级补足。300476 (industry='元件') 这种 CNINFO 细分行业池子
            # 里可能就本股自己,严格匹配会一直空。fallback 至少给 LLM 一些
            # 可比锚 (按 PE 接近 + 市值 0.3-3x 范围筛)。
            cross_peers: list = []
            if len(same_industry_peers) < 3 and s.market_cap:
                exclude_codes = [p.code for p in same_industry_peers] + [s.code]
                needed = 5 - len(same_industry_peers)
                subq2 = (
                    db.query(Snapshot.code, func.max(Snapshot.id).label("max_id"))
                    .filter(
                        Snapshot.code.notin_(exclude_codes),
                        Snapshot.pe_ratio.isnot(None),
                        Snapshot.market_cap.isnot(None),
                        Snapshot.market_cap.between(
                            s.market_cap / 3, s.market_cap * 3
                        ),
                    )
                    .group_by(Snapshot.code).subquery()
                )
                cross_peers = (
                    db.query(Snapshot)
                    .join(subq2, Snapshot.id == subq2.c.max_id)
                    .order_by(func.abs(Snapshot.pe_ratio - self_pe).asc())
                    .limit(needed).all()
                )
        finally:
            db.close()
    except Exception as e:
        logger.warning("peer comparison: failed for %s (%s); skipping", s.code, e)
        return ""

    all_peers = list(same_industry_peers) + list(cross_peers)
    if len(all_peers) < 1:
        # 完全没可比 (本股无市值且同行业 0 支),放弃
        return ""

    industry_label = s.industry_name or "未分类"
    if cross_peers:
        title = (
            f"\n## 同业可比 (行业「{industry_label}」内 {len(same_industry_peers)} 支 "
            f"+ 跨行业市值同量级 PE 接近 {len(cross_peers)} 支)"
        )
    else:
        title = f"\n## 同业可比 (行业「{industry_label}」内 PE 最接近本股的 {len(all_peers)} 支)"
    lines = [title, "格式: 代码 / PE / PB / 今日%"]
    peers = all_peers
    for p in peers:
        pe = f"{p.pe_ratio:.1f}" if p.pe_ratio is not None else "—"
        pb = f"{p.pb_ratio:.2f}" if p.pb_ratio is not None else "—"
        cp = f"{p.change_pct:+.2f}%" if p.change_pct is not None else "—"
        lines.append(f"- {p.code}: PE {pe} / PB {pb} / {cp}")

    # 本股对照
    self_pe_str = f"{s.pe_ratio:.1f}" if s.pe_ratio is not None else "—"
    self_pb_str = f"{s.pb_ratio:.2f}" if s.pb_ratio is not None else "—"
    self_cp_str = f"{s.change_pct:+.2f}%" if s.change_pct is not None else "—"
    lines.append(f"- **本股 {s.code}: PE {self_pe_str} / PB {self_pb_str} / {self_cp_str}**")
    # A: 强引导,告诉 LLM **必须**在 deep_analysis 里引用具体可比股票代码,
    # 不要只说"行业均值"。
    lines.append(
        "**必读使用方式**:在 deep_analysis 的「看多 vs 看空」或「股价剧情」段,"
        "至少引用 1-2 支具体可比股票代码 + PE 数字做对比 (例如 '本股 PE 113,"
        f"同业 {peers[0].code} 是 {peers[0].pe_ratio:.0f}、{peers[1].code} 是 {peers[1].pe_ratio:.0f},"
        "本股居中/偏高/偏低')。不要只说'行业均值',那是抽象数字,具体可比股票"
        "才是相对估值锚。"
    )
    return "\n".join(lines) + "\n"


def _market_and_sector_context(industry_name: str | None) -> str:
    """Build the 大盘+板块 prompt section. Fail-safe: returns '' on any
    upstream error so a transient sector/index fetch failure doesn't
    abort the analysis."""
    from . import market as market_svc
    from . import sectors as sectors_svc
    try:
        indices = market_svc.get_indices()
    except Exception as e:
        logger.warning("market context: get_indices failed (%s); skipping", e)
        indices = []

    sector_line = ""
    if industry_name:
        try:
            sectors_list = sectors_svc.get_sectors()
            match = next((s for s in sectors_list if s.get("name") == industry_name), None)
            if match and match.get("change_pct") is not None:
                cp = match["change_pct"]
                sector_line = f"- 所属板块「{industry_name}」: {cp:+.2f}%\n"
        except Exception as e:
            logger.warning("market context: get_sectors failed (%s); skipping sector line", e)

    if not indices and not sector_line:
        return ""

    lines = ["\n## 大盘与板块表现(今日)"]
    for ix in indices:
        lines.append(
            f"- {ix['name']}: {ix['point']:.2f} 点 ({ix['change_pct']:+.2f}%)"
        )
    if sector_line:
        lines.append(sector_line.rstrip())
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Freshness logic — shared between the smart intraday cron and the list
# view's is_fresh badge. The cron uses _should_reanalyze() to decide
# whether to fire LLM; the list view uses the same logic to decide
# whether to flag a row as "已过期" — making the two systems agree.
# ---------------------------------------------------------------------------

# Threshold for "price moved enough to re-analyze" (% relative to anchor).
# 1.5% per heavy-user feedback (6/3) — A股 日均振幅 2-3%, 1.5% 捕捉到 ~30-40
# 支变化的票而不是只有龙虎榜级波动。
SMART_PRICE_DELTA_PCT = 1.5

# Time-based fallback: even with no significant change, anything older than
# this is considered stale (and the smart cron will repaint it).
SMART_STALE_HOURS = 4

# Cooldown: existing analysis 不到这个时间内,smart cron 不再触发(防止
# 刚手动重生过的体验"刚解析又被刷")。NOTE: cooldown 不会让 is_fresh
# 变 False — 它只是 cron 的礼貌,UI 该说过期还得说过期。
SMART_COOLDOWN_MIN = 30


def should_reanalyze(
    snap: "Snapshot | None",
    existing: "Analysis | None",
    anchor_snap: "Snapshot | None",
    *,
    respect_cooldown: bool = True,
) -> tuple[bool, str]:
    """Decide whether smart batch should re-analyze (or, with cooldown
    disabled, whether the list view should call this row "已过期").

    Returns (should, reason_tag). reason_tag is one of:
      - cooldown / no_snap / no_existing / no_change / no_anchor
        / price_move / signal_change / stale

    `respect_cooldown` defaults True for the cron (don't bother刚解析的
    code); list view passes False so a row that's 5 min old but has a
    valid trigger condition still gets the visual badge.
    """
    if snap is None or snap.price is None:
        return False, "no_snap"
    if existing is None:
        return False, "no_existing"

    now = datetime.now(timezone.utc)
    created = (existing.created_at if existing.created_at.tzinfo
               else existing.created_at.replace(tzinfo=timezone.utc))
    age = now - created

    if respect_cooldown and age < timedelta(minutes=SMART_COOLDOWN_MIN):
        return False, "cooldown"

    if existing.snapshot_id == snap.id:
        return False, "no_change"

    if anchor_snap is not None and anchor_snap.price:
        pct = abs((snap.price - anchor_snap.price) / anchor_snap.price * 100)
        if pct >= SMART_PRICE_DELTA_PCT:
            return True, "price_move"
    elif existing.snapshot_id is None:
        return True, "no_anchor"

    if anchor_snap is not None:
        old_sig = set(anchor_snap.signals or [])
        new_sig = set(snap.signals or [])
        if old_sig != new_sig:
            return True, "signal_change"

    if age > timedelta(hours=SMART_STALE_HOURS):
        return True, "stale"

    return False, "no_change"


def _data_completeness_prompt_section(comp: dict[str, Any]) -> str:
    """Render the data-completeness section so the LLM can self-calibrate
    confidence. Always rendered (even at score=100) so the LLM is
    consistently aware of this signal.

    5/28 措辞调整: 早期版本 prompt 太狠("数据严重不全,confidence 应该<60,
    并在 one_line_reason 体现"),LLM 不仅压低置信,还把"数据不全/缺失"
    塞进 company_tag 和 one_line_reason —— 用户看到一串"PCB龙头+...+数据
    不全"非常不友好。改为:
      - 用语温和,只描述事实(缺失/过期/盘中暂无),不强行规定 confidence
        阈值,让 LLM 自己结合其它信号判断
      - 明确禁止把"数据不全/缺失"等元信息写进 company_tag / one_line_reason
        这两个用户可见字段
    """
    lines = [f"\n## 输入数据状态\n完整度: {comp['score']}/100"]
    if comp.get("missing"):
        lines.append(f"缺失维度: {' · '.join(comp['missing'])}")
    if comp.get("intraday_skipped"):
        lines.append(
            f"盘中暂无(收盘后才齐): {' · '.join(comp['intraday_skipped'])} "
            f"—— 数据源属性,非输入质量问题"
        )
    if comp.get("stale"):
        lines.append(f"过期: {' · '.join(comp['stale'])}")
    # 6/9: info_missing — 可选信号缺失,不算 score 但告诉 LLM 哪些
    # 段会比较短或没内容,以便它自己权衡 confidence 上限。
    if comp.get("info_missing"):
        lines.append(
            f"可选信号缺失(不影响 score,但 confidence 上限自然受限): "
            f"{' · '.join(comp['info_missing'])}"
        )
    # Soft guidance — let the LLM decide based on the rest of the signal
    # stack rather than hard-anchoring confidence to a score threshold.
    lines.append(
        "请基于以上已有维度做判断,信号充分的维度正常打分。"
        "完整度低且关键维度缺失时再下调 confidence。"
        "**不要**把「数据不全」「数据缺失」「缺资金」「报告日期解析失败」"
        "「系统提示」等元信息或调试性文字写进 company_tag / one_line_reason "
        "/ deep_analysis —— 这些都是给用户看的内容,应该只描述公司/股票本身"
        "的特征,不暴露系统内部状态。"
    )
    return "\n".join(lines) + "\n"


# Tool schema. Claude is forced to call this; we read the structured input
# back as our analysis. The `additionalProperties: False` constraint + enums
# keep the model from drifting out of contract.
#
# v2 (4/27): expanded to match the editorial template the user shared
# (益佰制药 example): structured red-flag detection, multi-tier stop-loss
# levels, scenario-based action advice, 8-dimension risk scorecard.
ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "提交对该 A 股标的的结构化投资建议。必须调用一次。",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        # analysis_thinking comes first so the model fills it before any
        # structured field — JSON tool calls tend to honor declaration order
        # in practice. This is our chain-of-thought scratchpad: the model
        # is forced to reason before emitting buy/sell/position numbers,
        # which significantly improves answer quality. Stripped before
        # persistence so it doesn't pollute the deep_analysis display.
        "required": ["analysis_thinking", "key_table", "deep_analysis"],
        "properties": {
            "analysis_thinking": {
                "type": "string",
                "minLength": 200,
                "maxLength": 2000,
                "description": (
                    "你的分析思考过程，200-2000 字。"
                    "先看技术面（趋势 / 量价 / 关键支撑阻力）、再看资金面"
                    "（主力净流入、3 日累计、北向、龙虎榜）、再看消息面"
                    "（业绩 / 公告 / 新闻）、最后综合给方向。"
                    "把推理写出来，结构化字段在下面填——不要在这里给最终结论。"
                ),
            },
            "key_table": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "company_tag",
                    "actionable", "one_line_reason",
                    "red_flags",
                    "buy_price_low", "buy_price_high",
                    "sell_price_low", "sell_price_high",
                    "position_pct", "hold_period",
                    "stop_loss_levels",
                    "scenario_advice",
                    "actionable_tiers",
                    "next_day_outlook",
                    "risk_scores",
                    "confidence",
                    "confidence_reason",
                    "valid_window",
                ],
                "properties": {
                    "company_tag": {
                        "type": "string",
                        "description": (
                            '一句话公司画像，30 字内。格式参考："贵州中药老字号 + '
                            '业绩塌方 + 维权标记" 这种用 "+" 串起来的特征拼接。'
                        ),
                    },
                    "actionable": {
                        "type": "string",
                        "enum": ["建议买入", "观望", "建议卖出", "不建议入手"],
                    },
                    "one_line_reason": {
                        "type": "string",
                        "description": "一句话理由，不超过 40 字。直说，不要和稀泥。",
                    },
                    "red_flags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "硬标识检测，每条 ≤ 25 字。常见红旗：ST 标记 / 维权标记 / "
                            "曾被监管处罚 / 异常波动公告但公司声明无信息 / 由盈转亏 / "
                            "营收同比转负 / 三费占营收比 > 50% / 控股股东减持 / "
                            "限售解禁压力。没有就空数组。"
                        ),
                    },
                    "buy_price_low": {"type": "number", "description": "合理买入价区间下限"},
                    "buy_price_high": {"type": "number", "description": "合理买入价区间上限"},
                    "sell_price_low": {"type": "number", "description": "合理卖出价区间下限"},
                    "sell_price_high": {"type": "number", "description": "合理卖出价区间上限"},
                    "position_pct": {
                        "type": "number", "minimum": 0, "maximum": 100,
                        "description": "建议仓位百分比 (0-100)",
                    },
                    "hold_period": {
                        "type": "string",
                        "enum": ["短线 1-2周", "中线 1-3月", "长线 6月+"],
                    },
                    "stop_loss_levels": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 3,
                        "description": (
                            "止损线分档。最少 1 档（紧急），最多 3 档（紧急/中线/深跌）。"
                            "高危票应给齐 3 档，普通票 1-2 档即可。"
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["price", "label", "reason"],
                            "properties": {
                                "price": {"type": "number"},
                                "label": {
                                    "type": "string",
                                    "enum": ["紧急止损", "中线止损", "深跌止损"],
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "为什么是这个价 + 跌破后该做什么。≤ 50 字。",
                                },
                            },
                        },
                    },
                    "scenario_advice": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "not_holding",
                            "holding_big_gain",
                            "holding_small",
                            "holding_big_loss",
                        ],
                        "description": "按持仓情境给的具体动作建议，每条 ≤ 40 字。",
                        "properties": {
                            "not_holding": {
                                "type": "string",
                                "description": "未持仓的人怎么做。",
                            },
                            "holding_big_gain": {
                                "type": "string",
                                "description": "已持仓且大幅浮盈怎么做。",
                            },
                            "holding_small": {
                                "type": "string",
                                "description": "已持仓且小幅浮盈/浮亏怎么做。",
                            },
                            "holding_big_loss": {
                                "type": "string",
                                "description": "已持仓且大幅浮亏怎么做。",
                            },
                        },
                    },
                    "actionable_tiers": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["aggressive", "neutral", "conservative"],
                        "description": (
                            "三档操作建议——同一股、不同风险偏好。每档给出独立的"
                            "action / position / 价格区间 / reason。aggressive 比 neutral "
                            "更敢，conservative 更保守。三档之间的 position_pct 应递减"
                            "（aggressive ≥ neutral ≥ conservative），不应该出现 conservative "
                            "比 aggressive 还重仓的情况。reason ≤ 30 字。"
                        ),
                        "properties": {
                            f: {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "action", "position_pct",
                                    "buy_price_low", "buy_price_high",
                                    "hold_period", "reason",
                                ],
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["建议买入", "观望", "建议卖出", "不建议入手"],
                                    },
                                    "position_pct": {
                                        "type": "number", "minimum": 0, "maximum": 100,
                                    },
                                    "buy_price_low": {"type": "number"},
                                    "buy_price_high": {"type": "number"},
                                    "hold_period": {
                                        "type": "string",
                                        "enum": ["短线 1-2周", "中线 1-3月", "长线 6月+"],
                                    },
                                    "reason": {"type": "string", "description": "≤ 30 字"},
                                },
                            }
                            for f in ("aggressive", "neutral", "conservative")
                        },
                    },
                    "next_day_outlook": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["trend", "target_low", "target_high",
                                     "reasoning", "confidence"],
                        "description": (
                            "次日（下一交易日）走势预判。基于技术面（K 线 / MA / "
                            "MACD / RSI / KDJ / BOLL）+ 资金面（3 日净流入 / "
                            "今日主力 / 北向）+ 消息面给出 1-2 个交易日的预期。"
                            "信号不足就 confidence='低' + reasoning 写明哪里不足，"
                            "不要硬猜。"
                        ),
                        "properties": {
                            "trend": {
                                "type": "string",
                                "enum": ["看涨", "看平", "看跌"],
                            },
                            "target_low": {
                                "type": "number",
                                "description": "次日合理价格区间下限",
                            },
                            "target_high": {
                                "type": "number",
                                "description": "次日合理价格区间上限",
                            },
                            "reasoning": {
                                "type": "string",
                                "description": "≤ 80 字。这个判断的主要依据。",
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["高", "中", "低"],
                            },
                        },
                    },
                    "risk_scores": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "fundamentals", "valuation", "earnings_momentum",
                            "industry", "governance", "price_action",
                            "capital", "thematic", "overall",
                        ],
                        "description": (
                            "8 个维度评分（1-5 ⭐），加一个综合等级。"
                            "评分时不要平均化，差就是差，好就是好。"
                        ),
                        "properties": {
                            "fundamentals":     {"type": "integer", "minimum": 1, "maximum": 5, "description": "基本面"},
                            "valuation":        {"type": "integer", "minimum": 1, "maximum": 5, "description": "估值"},
                            "earnings_momentum":{"type": "integer", "minimum": 1, "maximum": 5, "description": "业绩兑现节奏"},
                            "industry":         {"type": "integer", "minimum": 1, "maximum": 5, "description": "行业景气度"},
                            "governance":       {"type": "integer", "minimum": 1, "maximum": 5, "description": "公司治理"},
                            "price_action":     {"type": "integer", "minimum": 1, "maximum": 5, "description": "股价表现"},
                            "capital":           {"type": "integer", "minimum": 1, "maximum": 5, "description": "资金面"},
                            "thematic":         {"type": "integer", "minimum": 1, "maximum": 5, "description": "题材炒作"},
                            "overall": {
                                "type": "string",
                                "enum": [
                                    "⭐ 极差", "⭐⭐ 较差", "⭐⭐⭐ 一般",
                                    "⭐⭐⭐⭐ 较好", "⭐⭐⭐⭐⭐ 极好",
                                ],
                                "description": "综合评级",
                            },
                        },
                    },
                    # Top-level confidence: 0-100. Was enum 高/中/低 pre-5/28;
                    # split into a numeric value + separate reason so we can
                    # do continuous visual degradation (e.g. <60 = dashed
                    # border + "慎跟" hint) and later correlate with hit_rate
                    # at finer granularity. Historical rows migrated via
                    # /api/_diag/migrate-confidence-to-int (高→85, 中→65,
                    # 低→45). next_day_outlook.confidence below remains an
                    # enum — different semantic (次日走势的把握), no need
                    # to unify.
                    "confidence": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": (
                            "对当前 actionable 判断的把握程度，0-100 整数。"
                            "综合：数据完整度（见 prompt 末尾的「## 数据完整度」段）"
                            "+ 信号方向一致性（技术/资金/基本面是否互相佐证）"
                            "+ 量价是否极端。常见档位参考：≥80 高把握；60-79 中等；"
                            "<60 信号弱或冲突。低于 60 时 one_line_reason 应措辞谨慎，"
                            "或考虑把 actionable 降为「观望」。"
                        ),
                    },
                    "confidence_reason": {
                        "type": "string",
                        "description": (
                            "1 句话说明置信打分依据，≤30 字。例：'量价齐升+财报新鲜' / "
                            "'资金流缺失，仅技术面' / '行业逆风，方向冲突'。"
                            "目的是让用户/复盘者看到这个分数是怎么来的。"
                        ),
                    },
                    # 5/29: explicit validity window. Heavy user reported
                    # not knowing how long a verdict is meant to apply —
                    # is "建议买入" valid for today, this week, until what
                    # price? Make the LLM declare it.
                    "valid_window": {
                        "type": "string",
                        "description": (
                            "本建议的参考有效窗口。要具体,不要写'近期'/'短期'这类含糊词。"
                            "例:'3 个交易日内' / '跌破 X.XX 元前' / '本周内' / '出新公告前'。"
                            "事件驱动型给事件触发,技术面驱动给价位触发,纯波段给天数。"
                            "如果没法说出具体窗口,给'1-3 个交易日内'保底,不要省略。"
                        ),
                    },
                },
            },
            "deep_analysis": {
                "type": "string",
                "description": (
                    "1500-2500 字 markdown 深度分析，按以下章节展开（每节用 ## 标题）：\n"
                    "## 公司画像\n"
                    "## 最新业绩\n"
                    "## 股价剧情\n"
                    "## 看多 vs 看空\n"
                    "## 风险与止损\n"
                    "## 操作建议\n"
                    "## 一句话总结\n\n"
                    "**风格要求（必须遵守）**：\n"
                    "1. 大白话，第一人称对您说，像跟朋友聊天\n"
                    "2. 直接表态：'清仓' / '不要补仓' / '别参与'，不写'谨慎' / '建议' / '可关注' 这类和稀泥的词\n"
                    "3. 关键数字 **加粗**，关键风险用 ⚠️，硬红灯用 🔴\n"
                    "4. 用有序列表 (1./2./3.) 拆步骤，无序列表 (-) 列要点\n"
                    "5. 不堆术语：把 PE / MACD / 主力净流入 / 北向资金 这种翻译成人话\n"
                    "6. 信息缺失就直说'信息不全，无法判断'，不要硬编"
                ),
            },
        },
    },
}


def _system_prompt(strategy: Strategy) -> list[dict[str, Any]]:
    """Returns Anthropic-format system blocks. The static parts use prompt
    caching so re-using the same strategy is cheap."""
    base = (
        "你是一位经验老到的 A 股投资顾问，目标用户是一支 10 人左右的小型投资团队。\n"
        "你的任务是基于 snapshot 给出**结构化、可执行**的投资建议。\n"
        "\n"
        "# 风格（很重要，请严格遵守）\n"
        "\n"
        "1. **大白话**。把 'PE / MACD / 主力净流入 / 北向资金 / 三费' 这种术语翻译成人话再说。\n"
        "2. **第一人称对话**。'我看下来这只票...'、'您如果已经持仓...'、'我建议您...'，像跟朋友聊天。\n"
        "3. **直接表态，不和稀泥**。说 '清仓' / '不要补仓' / '别参与'，不要说 '谨慎' / '可关注' / '建议结合自身情况'。\n"
        "4. **关键数字加粗**（**+8.13%**、**-2.7亿**），关键风险用 ⚠️，硬红灯用 🔴。\n"
        "5. **有结构有层次**。用有序列表（1./2./3.）拆步骤，无序列表（-）列要点。\n"
        "\n"
        "# 必须做的红旗检查清单\n"
        "\n"
        "发起分析前，按这个清单逐条核对，命中的填进 red_flags（每条 ≤ 25 字）：\n"
        "- 🔴 名称带 ST / 维权（'（维权）' 标记）\n"
        "- 🔴 曾被监管处罚（信息中含 '立案' / '通知书' / '暂停' / '处罚' 等）\n"
        "- 🔴 业绩由盈转亏 / 营收同比转负\n"
        "- 🔴 异常波动公告但公司声明 '无重大信息'\n"
        "- 🔴 三费占营收比 > 50%\n"
        "- 🔴 控股股东减持 / 大额限售解禁\n"
        "没命中就空数组，不要凑数。\n"
        "\n"
        "# 数据原则\n"
        "\n"
        "- 完全基于提供的 snapshot 与新闻 / 公告做判断，**不要编造**未在输入中的信息\n"
        "- 关键信号缺失（如技术面、财报）时降低 confidence；但 company_tag / "
        "one_line_reason 是给用户看的标签，永远只描述股票本身特征，不要把"
        "「数据不全 / 缺失」之类的元信息写进去\n"
        "- 始终调用 submit_analysis 工具**一次**，不要给出其他文本\n"
        "- **先填 analysis_thinking 字段把推理写完，再填 key_table 和 deep_analysis 的结构化结果**。\n"
        "  不允许跳过 analysis_thinking 直接给结论。这是为了让你思考更充分，质量更高。\n"
        "\n"
        "# 次日走势预判（next_day_outlook）\n"
        "\n"
        "基于技术面 + 资金面 + 消息面，给出**下一交易日**的走势预判。\n"
        "- trend: 看涨 / 看平 / 看跌\n"
        "- target_low / target_high: 合理价格区间（基于支撑位 / 阻力位 / 当日波幅）\n"
        "- reasoning ≤ 80 字: 这个判断的主要依据\n"
        "- confidence: 信号充分→高；模糊→中；信号不足→低（且 reasoning 写明哪里不足）\n"
        "\n"
        "**别硬猜**。技术面信号缺失（KK 线没拉到）+ 当日没特殊消息 → confidence 直接给 '低'，"
        "trend 给 '看平'，target 给一个相对窄的区间。\n"
        "\n"
        "# 三档建议（actionable_tiers）\n"
        "\n"
        "同一支票，不同人风险偏好不同。除了顶部的 actionable / position_pct 给"
        "中立读者，还要分别给出三档操作：\n"
        "- **aggressive**：愿意承担更大波动换收益，仓位更重、买入区间可上探\n"
        "- **neutral**：标准用户，应与顶部 actionable + position_pct 一致\n"
        "- **conservative**：风险厌恶，仓位更轻、买入区间更低或建议观望\n"
        "\n"
        "**硬约束**：position_pct 必须 aggressive ≥ neutral ≥ conservative。同一票"
        "在三档之间应该是连续的——不要 aggressive=买入 50%、conservative=买入 80%"
        "这种自相矛盾。每档 reason ≤ 30 字。\n"
        "\n"
        "# 建议有效期(valid_window)\n"
        "\n"
        "必填字段。要诚实+具体,不要写'近期'/'短期'这类废话:\n"
        "- 事件驱动型(等业绩 / 出公告 / 解禁) → 给事件触发,例'出 Q3 财报前'\n"
        "- 技术面驱动型(关键支撑阻力位) → 给价位触发,例'跌破 X.XX 前'\n"
        "- 纯波段(无明确事件/技术位) → 给天数,例'3 个交易日内'\n"
        "无论哪种,用户应当一眼能判断'什么情况下这个建议作废'。\n"
    )
    rules_section = ""
    if strategy.rules:
        rules_section = (
            "\n\n# 必须遵守的硬规则（来自策略）\n"
            + "\n".join(f"- {r}" for r in strategy.rules)
            + "\n违反任何一条都应直接给出 actionable=不建议入手。"
        )
    return [{
        "type": "text",
        "text": base + rules_section,
        "cache_control": {"type": "ephemeral"},
    }]


def _user_prompt(
    w: Watchlist,
    s: Snapshot | None,
    data_completeness: dict[str, Any] | None = None,
) -> str:
    """Build the user message.

    `data_completeness` is the dict from compute_data_completeness(); when
    passed (the normal path from generate()), we append a 数据完整度 section
    so the LLM can self-calibrate. The 大盘/板块 section is also appended
    here unconditionally (fail-safe inside _market_and_sector_context).

    Kept as a default-None param rather than a required one so callers
    that just need the base snapshot rendering (e.g. analysis_debate's
    bull/bear roles which reuse the base prompt) don't have to compute
    completeness separately. They still get the market/sector context.
    """
    # 行业池太薄(同业 <3 支)时,industry_pe_avg 基本就是本股自己在
    # 自我印证("行业均值 69.7"=自己的 PE),分位恒为 0/100 — 这种数字
    # 喂给 LLM 是误导锚而不是信息。peer_count=None(旧调用方/无行业)
    # 时保持原行为不抑制。
    _pc = (data_completeness or {}).get("peer_count")
    show_industry_ctx = not (_pc is not None and _pc < 3)

    if s is None:
        snap_section = "（暂无 snapshot 数据，请基于代码、名称、市场常识做最低限度的判断，并显著降低置信度。）"
    else:
        news_lines = "\n".join(
            f"- [{n.get('ts','')}] {n.get('title','')}" for n in (s.news or [])[:5]
        ) or "（无）"
        notice_lines = "\n".join(
            f"- [{n.get('ts','')}] {n.get('title','')}（{n.get('type') or '一般'}）"
            for n in (s.notices or [])[:5]
        ) or "（无）"
        lhb = (
            f"上榜原因={s.lhb.get('reason','')}, 净买额={s.lhb.get('net_buy')}"
            if s.lhb else "（今日未上榜）"
        )
        # Estimate units to keep the LLM grounded — bare numbers like
        # "1757186000000" are easy to misread; "1.76 万亿" is hard to misread.
        def _yi(v: float | None) -> str:
            if v is None:
                return "未知"
            return f"{v / 1e8:.1f} 亿"

        valuation_section = (
            f"市盈率(PE,动态): {s.pe_ratio if s.pe_ratio is not None else '未知'}"
            + (f"  (行业均值 {s.industry_pe_avg:.1f}, 本股分位 {s.industry_pe_pctile:.0f}%)"
               if show_industry_ctx and s.industry_pe_avg is not None
               and s.industry_pe_pctile is not None else "")
            + "\n"
            + f"市净率(PB): {s.pb_ratio if s.pb_ratio is not None else '未知'}"
            + (f"  (行业均值 {s.industry_pb_avg:.2f})"
               if show_industry_ctx and s.industry_pb_avg is not None else "")
            + "\n"
            + f"换手率: {f'{s.turnover_rate:.2f}%' if s.turnover_rate is not None else '未知'}\n"
            + f"总市值: {_yi(s.market_cap)}\n"
            + f"流通市值: {_yi(s.circ_market_cap)}\n"
        )
        # Phase 7: 3-day rolling metrics, used by the LLM to spot trends
        # the daily snapshot misses. Rendered only when at least one field
        # is present so the prompt doesn't grow noise on cold-start codes.
        three_day_section = ""
        if (s.change_pct_3d is not None or s.turnover_rate_3d is not None or
                s.net_flow_3d is not None):
            three_day_section = (
                f"\n## 近3日表现\n"
                + f"3日涨幅: {f'{s.change_pct_3d:+.2f}%' if s.change_pct_3d is not None else '未知'}"
                + (f"  (行业分位 {s.industry_change_3d_pctile:.0f}%)"
                   if show_industry_ctx and s.industry_change_3d_pctile is not None else "")
                + "\n"
                + f"3日累计换手率: {f'{s.turnover_rate_3d:.2f}%' if s.turnover_rate_3d is not None else '未知'}\n"
                + f"3日主力净流入: {_yi(s.net_flow_3d) if s.net_flow_3d is not None else '未知'}"
                + (f"  (行业分位 {s.industry_flow_3d_pctile:.0f}%)"
                   if show_industry_ctx and s.industry_flow_3d_pctile is not None else "")
                + "\n"
            )
        industry_line = (
            f"所属行业: {s.industry_name}\n" if s.industry_name else ""
        )
        # Phase 9: technical-面 block fed by latest K-line + indicators.
        # Falls back to "未拉到" when the post-close kline tick hasn't run
        # yet for this code (cold start) — LLM is told to drop confidence.
        from . import kline as kline_svc  # late import to dodge circular
        latest_k = kline_svc.latest_for_code(s.code)
        if latest_k is None:
            technical_section = (
                "\n## 技术面\n（K 线未拉到，技术面信号缺失，"
                "请把 next_day_outlook.confidence 设为 '低'）\n"
            )
        else:
            def _f(v):
                return f"{v:.2f}" if v is not None else "—"
            macd_status = "?"
            if (latest_k.macd_dif is not None and latest_k.macd_dea is not None):
                macd_status = "DIF 上 DEA" if latest_k.macd_dif > latest_k.macd_dea else "DIF 下 DEA"

            # Compact 20-day K-line block. Gives the LLM trend / box /
            # vol-price-divergence visibility instead of just one anchored
            # snapshot. ~20 rows × 6 cols of digits is well within token
            # budget for kimi-k2.5's 16K context.
            recent = kline_svc.recent_for_code(s.code, days=20)
            history_lines = ""
            position_note = ""
            if recent:
                # Find prev high / low and how many trading days ago
                closes_with_idx = [(i, r.close) for i, r in enumerate(recent)
                                   if r.close is not None]
                if closes_with_idx:
                    hi_idx, hi_val = max(closes_with_idx, key=lambda x: x[1])
                    lo_idx, lo_val = min(closes_with_idx, key=lambda x: x[1])
                    days_from_high = len(recent) - 1 - hi_idx
                    days_from_low = len(recent) - 1 - lo_idx
                    position_note = (
                        f"近 20 日最高 {hi_val:.2f}（{days_from_high} 日前），"
                        f"最低 {lo_val:.2f}（{days_from_low} 日前）\n"
                    )

                def _fmt_vol(v):
                    if v is None:
                        return "—"
                    if v >= 1e8:
                        return f"{v/1e8:.2f}亿"
                    if v >= 1e4:
                        return f"{v/1e4:.0f}万"
                    return f"{v:.0f}"
                header = "日期         开    收    高    低    量\n"
                rows = "\n".join(
                    f"{r.date}  {_f(r.open):>5}  {_f(r.close):>5}  "
                    f"{_f(r.high):>5}  {_f(r.low):>5}  {_fmt_vol(r.volume):>7}"
                    for r in recent
                )
                history_lines = (
                    f"\n近 {len(recent)} 日 K 线（升序）：\n{header}{rows}\n"
                    f"{position_note}"
                )

            technical_section = (
                f"\n## 技术面（{latest_k.date}）\n"
                f"收盘: {_f(latest_k.close)}\n"
                f"MA5/10/20/60: {_f(latest_k.ma5)} / {_f(latest_k.ma10)} / "
                f"{_f(latest_k.ma20)} / {_f(latest_k.ma60)}\n"
                f"MACD: DIF={_f(latest_k.macd_dif)}  DEA={_f(latest_k.macd_dea)}  "
                f"HIST={_f(latest_k.macd_hist)}  ({macd_status})\n"
                f"BOLL: 中={_f(latest_k.boll_mid)} 上={_f(latest_k.boll_up)} "
                f"下={_f(latest_k.boll_low)}\n"
                f"KDJ: K={_f(latest_k.kdj_k)} D={_f(latest_k.kdj_d)} J={_f(latest_k.kdj_j)}\n"
                f"RSI6 / RSI12: {_f(latest_k.rsi6)} / {_f(latest_k.rsi12)}\n"
                f"{history_lines}"
            )
        # Phase 10: financial-statement summary. Latest 2 quarters so the
        # LLM can see direction (improving vs deteriorating) rather than
        # an isolated snapshot.
        from . import financials as fin_svc
        fin_rows = fin_svc.latest_for_code(s.code, n=2)
        if fin_rows:
            def _yi(v):
                return f"{v/1e8:.1f}亿" if v else "—"
            def _pct(v):
                return f"{v:+.2f}%" if v is not None else "—"
            financials_section = "\n## 财务面\n"
            for i, f_row in enumerate(fin_rows):
                tag = "最新" if i == 0 else "上期对照"
                financials_section += (
                    f"{tag} ({f_row.report_date}): "
                    f"营收 {_yi(f_row.total_revenue)} (同比 {_pct(f_row.revenue_yoy)})  "
                    f"净利润 {_yi(f_row.net_profit)} (同比 {_pct(f_row.profit_yoy)})  "
                    f"毛利率 {_pct(f_row.gross_margin)}  净利率 {_pct(f_row.net_margin)}  "
                    f"ROE {_pct(f_row.roe)}  期间费用率 {_pct(f_row.expense_ratio)}\n"
                )
        else:
            financials_section = (
                "\n## 财务面\n（财报数据未拉到，请把基本面权重降低，"
                "并在 deep_analysis 里点出'缺财报数据'。）\n"
            )

        snap_section = (
            f"快照时间: {s.ts.isoformat()}\n"
            f"{industry_line}"
            f"最新价: {s.price}\n"
            f"涨跌幅: {s.change_pct}%\n"
            f"成交量: {s.volume}\n"
            f"成交额: {s.turnover} 元\n"
            f"主力净流入(当日): {s.main_net_flow} 元\n"
            f"命中信号: {', '.join(s.signals or []) or '（无）'}\n"
            f"{technical_section}\n"
            f"## 估值与活跃度\n{valuation_section}"
            f"{three_day_section}"
            f"{financials_section}\n"
            f"## 最近新闻\n{news_lines}\n\n"
            f"## 最近公告\n{notice_lines}\n\n"
            f"## 龙虎榜\n{lhb}\n"
        )

    # 6/9: 股东变动 + 同业可比 两段 — 填补 LLM 缺少的"内部人信号"+
    # "相对估值锚",目标是让 LLM 拿到更多硬信号后敢用 confidence 全区间。
    # 都是 fail-safe (内部 try/except),任何异常返回 '' 不阻塞 prompt。
    shareholder_section = _shareholder_changes_section(s.code if s else None)
    peer_section = _peer_comparison_section(s)

    # 大盘+板块 context. Fail-safe internally — returns '' on upstream error.
    market_ctx = _market_and_sector_context(s.industry_name if s else None)

    # 6/10: 主营业务 from CNINFO (cached in industry_meta). 公司画像此前
    # 完全依赖模型对公司的世界知识 — 小盘股/转型过主业的票,模型可能在
    # 用过时记忆编故事。给真实主营业务,把这块幻觉面收掉。Fail-safe:
    # 没有行就不渲染这行。
    business_line = ""
    try:
        from . import industry as industry_svc
        biz = industry_svc.get_business_desc(w.code)
        if biz:
            business_line = f"主营业务: {biz}\n"
    except Exception:
        pass

    # Data completeness section (only when caller provided the dict — i.e.
    # the normal generate() path; debate sub-roles reuse base_user and
    # don't need it duplicated).
    data_comp_section = (
        _data_completeness_prompt_section(data_completeness)
        if data_completeness is not None else ""
    )

    return (
        f"## 标的\n代码: {w.code}\n名称: {w.name}\n市场: {w.exchange}\n{business_line}\n"
        f"## 最新 snapshot\n{snap_section}\n"
        f"{shareholder_section}"
        f"{peer_section}"
        f"{market_ctx}"
        f"{data_comp_section}\n"
        f"请基于上面的信息调用 submit_analysis 一次。"
    )


def generate(
    db: Session,
    code: str,
    strategy_name: str | None = None,
    client: Anthropic | None = None,
    mode: str = "single",
    force: bool = False,
) -> Analysis:
    """Synchronously generate a fresh analysis and persist it.

    `mode`:
      - "single" (default): one LLM call. Fast, cheap, standard path.
      - "debate": runs the three-role bull/bear/judge pipeline. 3x LLM
        cost. Better red-flag detection for high-stakes calls. Triggered
        from the route by ?mode=debate or auto-promoted when the single-
        pass result is a high-conviction buy/sell (handled by caller; this
        function trusts the mode argument as given).

    `force` (5/29): when False (default), if the latest snapshot for this
    code matches the existing Analysis row's snapshot_id, we return the
    cached row without re-calling the LLM. Suppresses the "regenerate
    gives a different answer to the same input" complaint that came in
    from a heavy user: LLM sampling makes repeated calls give different
    verdicts on identical inputs, which destroys trust. Set force=True
    when the user explicitly clicks "重新生成" in the detail page (they
    want a fresh answer); batch_analyze leaves it False to dedupe.

    Replaces the existing Analysis row for this code (one row per code policy).
    """
    w = db.query(Watchlist).filter(Watchlist.code == code).first()
    if not w:
        raise ValueError(f"{code} not in watchlist")

    s = (
        db.query(Snapshot)
        .filter(Snapshot.code == code)
        .order_by(desc(Snapshot.id))
        .first()
    )

    # Cache hit: same snapshot, same code → reuse existing verdict
    # (assuming one exists). Saves both tokens AND user trust in the
    # consistency of identical inputs. force=True skips this branch.
    if not force and s is not None:
        existing_cached = db.query(Analysis).filter(Analysis.code == code).first()
        if existing_cached is not None and existing_cached.snapshot_id == s.id:
            logger.info(
                "analysis[%s] cache hit on snapshot_id=%d, skipping LLM (force=False)",
                code, s.id,
            )
            return existing_cached

    strat = get_strategy(strategy_name)
    if client is None:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Add it in Railway → backend → Variables."
            )
        # 6/4: timeout=120 + max_retries=0,配合 cron 层 ThreadPoolExecutor
        # 并发(max_workers=5)。
        #
        # 演化:
        #   - timeout=60 + SDK retry=2 → 6/4 11:05 cycle 67min 卡死
        #   - timeout=60 + max_retries=0 → 13:05 failed 77% (kimi p77 > 60s)
        #   - timeout=90 + max_retries=0 + 并发 → 14:05 OK,cycle 18 min
        #   - 6/9 timeout=90 还撞到 detail 页 force=true APITimeoutError
        #     (300476/688008 重新生成 60s 内没完成 — 可能是 6/9 加 peer
        #     强引导让 LLM 输出多+慢,或者 kimi 整体波动)。调到 120 让
        #     单股 call 更可能 cover p90 延迟。
        #
        # 100 stocks × 60% trigger × 120s / 5 并发 = 24 min,接近 30 min
        # cycle 边界。实际触发率通常 ~20%,20×120/5=8 min,充裕。
        kwargs: dict[str, Any] = {
            "api_key": settings.ANTHROPIC_API_KEY,
            "timeout": 120.0,
            "max_retries": 0,
        }
        if settings.ANTHROPIC_BASE_URL:
            kwargs["base_url"] = settings.ANTHROPIC_BASE_URL
        client = Anthropic(**kwargs)

    model = settings.ANALYSIS_MODEL or DEFAULT_MODEL

    # Compute data completeness once — fed into the prompt AND persisted on
    # the Analysis row so we can later correlate input quality with
    # hit-rate (e.g. low completeness analyses might be systematically off).
    data_comp = compute_data_completeness(s, code)
    logger.info(
        "data_completeness[%s] %d/100  missing=%s  stale=%s",
        code, data_comp["score"], data_comp["missing"], data_comp["stale"],
    )

    # Build the user message. In debate mode the bull/bear views are
    # appended after the base snapshot block so the judge sees both sides
    # without re-deriving them.
    base_user = _user_prompt(w, s, data_completeness=data_comp)
    debate_suffix = ""
    if mode == "debate":
        from .analysis_debate import run_debate, render_debate_for_judge, judge_system_prompt_suffix
        logger.info("analysis[%s] starting debate mode (3 LLM calls)", code)
        bull, bear = run_debate(client, model, w, s, base_user)
        debate_suffix = render_debate_for_judge(bull, bear)
        judge_system = (
            _system_prompt(strat)
            + [{"type": "text", "text": judge_system_prompt_suffix()}]
        )
        system_blocks = judge_system
    else:
        system_blocks = _system_prompt(strat)

    # tool_choice negotiation: most providers accept the strict
    # `{"type":"tool", "name":...}` shape, but some only support `any`/`auto`.
    # We try strict first, fall back to `any` on a 400 — for our single-tool
    # setup the two are functionally equivalent.
    base_kwargs = {
        "model": model,
        "max_tokens": 8192,  # bumped from 4096 — kimi/glm/minimax outputs run
                              # 1k–2k tokens; with thinking-style reasoners
                              # (qwen3.6) we'd hit ceiling at 4096 mid-tool.
        "system": system_blocks,
        "tools": [ANALYSIS_TOOL],
        "messages": [{"role": "user", "content": base_user + debate_suffix}],
    }
    def _call_once():
        """One LLM round-trip. Returns (msg, tool_use, error_tag) tuple.
        error_tag is None on success, or a short string when output is
        unusable (truncated JSON, missing tool_use, missing required
        fields) — caller decides whether to retry."""
        try:
            local_msg = client.messages.create(
                **base_kwargs,
                tool_choice={"type": "tool", "name": "submit_analysis"},
            )
        except Exception as e:
            if "tool_choice" in str(e) or "400" in str(e):
                logger.info("model %s rejected forced tool_choice; retrying with 'any'", model)
                local_msg = client.messages.create(**base_kwargs, tool_choice={"type": "any"})
            else:
                raise

        local_tu = next(
            (b for b in local_msg.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if local_tu is None:
            logger.warning(
                "no tool_use block. stop_reason=%s, usage=%s",
                getattr(local_msg, "stop_reason", None),
                getattr(local_msg, "usage", None),
            )
            return local_msg, None, "no_tool_use"

        local_payload = local_tu.input
        if not isinstance(local_payload, dict) or "key_table" not in local_payload \
                or "deep_analysis" not in local_payload:
            # 输出被截断/不完整。常见原因: stream 中断 (dashscope 偶发),
            # 或 LLM 自己写到一半停了。caller retry 1 次大概率 OK。
            raw = getattr(local_tu, "raw_arguments", None) or local_payload
            logger.warning(
                "tool_use incomplete. stop_reason=%s, usage=%s, payload_keys=%s, raw_start=%r",
                getattr(local_msg, "stop_reason", None),
                getattr(local_msg, "usage", None),
                list(local_payload.keys()) if isinstance(local_payload, dict) else type(local_payload).__name__,
                str(raw)[:200],
            )
            return local_msg, local_tu, "incomplete_input"

        return local_msg, local_tu, None

    # App-level retry: kimi/dashscope 偶发 stream 截断或 partial JSON,
    # 一次重试大概率成功。SDK 层 max_retries=0 不变 (避免 SDK retry
    # 内联 backoff 让单 call 时长爆增)。
    msg, tool_use, err_tag = _call_once()
    if err_tag is not None:
        logger.info("analysis[%s] %s on first try, retrying once...", code, err_tag)
        msg, tool_use, err_tag = _call_once()
        if err_tag is not None:
            raise RuntimeError(
                f"LLM output unusable after 1 retry (err={err_tag}). "
                f"stop_reason={getattr(msg, 'stop_reason', None)}"
            )

    payload: dict[str, Any] = tool_use.input  # type: ignore[assignment]

    # Strip analysis_thinking — it's the CoT scratchpad, useful for the
    # model but not for display / persistence. Log a snippet so we can
    # spot-check that the model actually used it.
    thinking = payload.pop("analysis_thinking", None)
    if thinking:
        logger.info(
            "analysis_thinking[%s] %d chars: %s",
            code, len(thinking), thinking[:120].replace("\n", " "),
        )

    # Post-hoc validators: catch self-contradictions the LLM still emits
    # (ST stocks given 观望 instead of 不建议入手, 建议买入 against broken
    # technicals, tier monotonicity violations, etc.) Mutates payload + adds
    # corrections to red_flags.
    from .analysis_validators import validate_and_correct
    validate_and_correct(payload, w, s)

    existing = db.query(Analysis).filter(Analysis.code == code).first()
    if existing:
        existing.key_table = payload["key_table"]
        existing.deep_analysis = payload["deep_analysis"]
        existing.snapshot_id = s.id if s else None
        existing.model = model
        existing.strategy = strat.name
        existing.prompt_version = prompt_version_for(mode)
        existing.mode = mode
        existing.data_completeness = data_comp["score"]
        existing.created_at = datetime.now(timezone.utc)
        row = existing
    else:
        row = Analysis(
            code=code,
            key_table=payload["key_table"],
            deep_analysis=payload["deep_analysis"],
            snapshot_id=s.id if s else None,
            model=model,
            strategy=strat.name,
            prompt_version=prompt_version_for(mode),
            mode=mode,
            data_completeness=data_comp["score"],
        )
        db.add(row)
    db.commit()
    db.refresh(row)

    # Drop an outcome anchor for the feedback loop — verdict + reference
    # price now, forward returns filled later by _outcomes_tick. Best-effort:
    # a failure here must not fail the analysis itself.
    try:
        from .outcomes import record_anchor
        kt = payload.get("key_table") or {}
        # 5/29: include confidence + data_completeness so the detail-page
        # 历史解析 card can plot how those values shifted across
        # regenerations. confidence in kt may be int (new) or legacy
        # enum string — record_anchor normalizes both.
        raw_conf = kt.get("confidence")
        conf_for_anchor: int | str | None = raw_conf if isinstance(raw_conf, (int, str)) else None
        # 6/10: capture next_day_outlook's claim so nd_outlook_stats can
        # score it against return_d1 — previously the most falsifiable
        # output of the product was never measured.
        nd = kt.get("next_day_outlook") or {}
        record_anchor(
            db, code=code, generated_at=row.created_at,
            actionable=str(kt.get("actionable") or ""),
            prompt_version=prompt_version_for(mode), mode=mode,
            anchor_price=(s.price if s else None),
            confidence=conf_for_anchor,  # type: ignore[arg-type]
            data_completeness=data_comp["score"],
            model=model,
            nd_trend=(nd.get("trend") if isinstance(nd, dict) else None),
            nd_confidence=(nd.get("confidence") if isinstance(nd, dict) else None),
        )
    except Exception:
        logger.exception("outcome anchor failed for %s (non-fatal)", code)

    return row


def get_cached(db: Session, code: str, max_age_hours: int = 4) -> Analysis | None:
    """Return cached analysis if it's still fresh AND on the v2 schema."""
    row = db.query(Analysis).filter(Analysis.code == code).first()
    if row is None:
        return None
    # Schema v2 invalidation: rows missing the new `company_tag` field were
    # generated against the old key_table schema and need to be regenerated.
    if not isinstance(row.key_table, dict) or "company_tag" not in row.key_table:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    created = row.created_at
    if created.tzinfo is None:  # SQLite returns naive
        created = created.replace(tzinfo=timezone.utc)
    if created < cutoff:
        return None
    return row
