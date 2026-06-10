import logging
import os
from contextlib import asynccontextmanager

# Silence akshare's per-call tqdm progress bars BEFORE akshare gets imported.
# Without this, every `stock_news_em` / `stock_notice_report` / `stock_lhb_*`
# call writes a multi-line progress bar to stderr; uvicorn tags everything on
# stderr as `[err]`, so logs look like a fire even when the job is healthy.
# Both the env var and the monkey-patch are belt-and-suspenders — different
# tqdm versions honor different things, akshare uses its own bundled tqdm in
# some places.
os.environ.setdefault("TQDM_DISABLE", "1")
try:
    from functools import partialmethod
    import tqdm as _tqdm
    _tqdm.tqdm.__init__ = partialmethod(_tqdm.tqdm.__init__, disable=True)
except Exception:  # pragma: no cover — defensive
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db import Base, engine, ensure_extra_columns, migrate_watchlist_pk, snapshot_columns
from .models import (  # noqa: F401  (register tables with metadata)
    Analysis, AnalysisOutcome, Financial, Holding, IndustryMeta, InviteCode,
    Kline, SectorPicks, Snapshot, User, Watchlist,
)
from .routes import auth as auth_routes
from .routes import holdings as holdings_routes
from .routes import market as market_routes
from .routes import sectors as sectors_routes
from .routes import stocks as stocks_routes
from .routes import watchlist as watchlist_routes
from .services.cron import start_scheduler, stop_scheduler
from .services.users import migrate_admin_watchlist

# Default Python logging swallows INFO; that hid `snapshot job: ...` and
# `scheduler started: ...` from Railway logs and made cron health hard to
# diagnose. Promote our own loggers to INFO without touching uvicorn's.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # MVP: create tables on startup. Switch to Alembic when we need column changes.
    Base.metadata.create_all(bind=engine)
    # create_all doesn't add columns to existing tables; close that gap.
    ensure_extra_columns()
    # Phase 6 fix: switch watchlist PK from `code` to synthetic `id` so
    # different users can own the same code. Must run BEFORE the admin
    # backfill (which inserts/updates rows) and AFTER ensure_extra_columns
    # (which adds the user_id column).
    migrate_watchlist_pk()
    # Phase 6: backfill watchlist.user_id for existing rows (idempotent).
    # Skipped silently when ADMIN_PHONE is empty.
    migrate_admin_watchlist()
    if settings.SCHEDULER_ENABLED:
        start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="rich backend", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.FRONTEND_ORIGIN.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_routes.router)
app.include_router(watchlist_routes.router)
app.include_router(stocks_routes.router)
app.include_router(sectors_routes.router)
app.include_router(holdings_routes.router)
app.include_router(market_routes.router)


@app.post("/api/_diag/refresh-industry-meta")
def diag_refresh_industry_meta():
    """One-shot industry mapping pull. Phase 7 stores per-stock 行业 in
    the industry_meta table; without this, the snapshot job has no map
    to compute industry percentiles + averages from. Call once after
    deploy (or after adding new stocks)."""
    from .services import industry as industry_svc
    return industry_svc.refresh_industry_meta()


_financials_lock = __import__("threading").Lock()
_financials_running = {"v": False, "last_result": None}


@app.post("/api/_diag/refresh-financials")
def diag_refresh_financials():
    """One-shot financials bootstrap, ASYNC. Pulls 8 quarters per code in
    the watchlist via akshare's stock_financial_abstract (sina). ~90s for
    a 60-code watchlist — Railway's HTTP proxy kills synchronous requests
    around 30s, so we run in a background thread and surface progress
    via /api/_diag/refresh-financials/status.

    Safe to re-run, upsert by (code, report_date). Already-running calls
    return {already_running: true} without firing a second job."""
    import threading
    from .services import financials as fin_svc

    with _financials_lock:
        if _financials_running["v"]:
            return {"started": False, "already_running": True}
        _financials_running["v"] = True
        _financials_running["last_result"] = None

    def _worker():
        try:
            result = fin_svc.pull_for_watchlist()
            _financials_running["last_result"] = result
        except Exception as e:
            _financials_running["last_result"] = {"error": str(e)}
        finally:
            with _financials_lock:
                _financials_running["v"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True}


@app.get("/api/_diag/refresh-financials/status")
def diag_refresh_financials_status():
    """Status of the most recent /refresh-financials run.

    Returns {running, progress, last_result}:
      - running: True while the worker is in flight
      - progress: live counters {done, ok, failed, total, current} so the
        client can show "5/61 done, currently fetching 600519" instead of
        a black-box spinner
      - last_result: None until the worker completes; afterwards holds
        the final counters dict or {error: msg}
    """
    from .services.financials import get_progress
    return {
        "running": _financials_running["v"],
        "progress": get_progress(),
        "last_result": _financials_running["last_result"],
    }


_outcomes_backfill_lock = __import__("threading").Lock()
_outcomes_backfill_running = {"v": False, "last_result": None}


@app.post("/api/_diag/backfill-outcomes")
def diag_backfill_outcomes():
    """Manually run the analysis-outcome backfill, ASYNC. Backfill walks
    the entire outcomes table (1k+ rows in prod) and runs a kline query
    per row — easily 60+ seconds, which Railway's HTTP proxy kills. So
    we fire a background thread and surface progress via the status
    endpoint, same shape as refresh-financials. Normal cadence is
    daily 17:00 BJT via _outcomes_tick; this endpoint is for ad-hoc
    catch-up runs after a long backend outage or schema change."""
    import threading
    from .services import outcomes as outcomes_svc

    with _outcomes_backfill_lock:
        if _outcomes_backfill_running["v"]:
            return {"started": False, "already_running": True}
        _outcomes_backfill_running["v"] = True
        _outcomes_backfill_running["last_result"] = None

    def _worker():
        try:
            result = outcomes_svc.backfill_outcomes()
            _outcomes_backfill_running["last_result"] = result
        except Exception as e:
            _outcomes_backfill_running["last_result"] = {"error": str(e)}
        finally:
            with _outcomes_backfill_lock:
                _outcomes_backfill_running["v"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True}


@app.get("/api/_diag/backfill-outcomes/status")
def diag_backfill_outcomes_status():
    """Status of the most recent /backfill-outcomes run."""
    return {
        "running": _outcomes_backfill_running["v"],
        "last_result": _outcomes_backfill_running["last_result"],
    }


@app.get("/api/_diag/outcomes-kline-coverage")
def diag_outcomes_kline_coverage():
    """Per-anchor inspection: for a sample of recent unscored outcomes,
    how many future kline bars are available? If most show future_bars=0,
    the kline_tick is not keeping their codes alive (e.g., user removed
    the stock from watchlist after the anchor was recorded).

    Returns the set of codes that have outcomes but no klines, and a
    small sample of (code, generated_at, future_bars) tuples."""
    from sqlalchemy import func as sa_func
    from .db import SessionLocal
    from .models import AnalysisOutcome, Kline
    db = SessionLocal()
    try:
        # All distinct codes the outcomes table tracks
        outcome_codes = {
            c for (c,) in db.query(AnalysisOutcome.code).distinct().all()
        }
        # All distinct codes that have any kline row
        kline_codes = {
            c for (c,) in db.query(Kline.code).distinct().all()
        }
        orphan_codes = sorted(outcome_codes - kline_codes)
        covered_codes = sorted(outcome_codes & kline_codes)

        # Sample 5 EARLIEST + 5 LATEST unscored outcomes — earliest ones
        # have had the most time for future bars to accumulate, so if even
        # those show future_bars=0 we've found a real bug (not just "kline
        # not yet caught up for fresh anchors").
        oldest = (
            db.query(AnalysisOutcome)
            .filter(AnalysisOutcome.return_d5.is_(None))
            .order_by(AnalysisOutcome.generated_at.asc())
            .limit(5)
            .all()
        )
        latest = (
            db.query(AnalysisOutcome)
            .filter(AnalysisOutcome.return_d5.is_(None))
            .order_by(AnalysisOutcome.generated_at.desc())
            .limit(5)
            .all()
        )
        sample = list(oldest) + list(latest)
        sample_detail = []
        for o in sample:
            gen_day = o.generated_at.date().isoformat()
            future_bars = db.query(sa_func.count(Kline.id)).filter(
                Kline.code == o.code,
                Kline.date > gen_day,
            ).scalar() or 0
            kline_for_code = db.query(sa_func.count(Kline.id)).filter(
                Kline.code == o.code,
            ).scalar() or 0
            sample_detail.append({
                "code": o.code,
                "actionable": o.actionable,
                "generated_at": o.generated_at.isoformat(),
                "gen_day_utc": gen_day,
                "kline_rows_for_code": kline_for_code,
                "future_bars_after_gen_day": future_bars,
                "close_d1": o.close_d1,
                "close_d3": o.close_d3,
                "close_d5": o.close_d5,
                "close_d20": o.close_d20,
            })

        # Per-horizon fill counts across the WHOLE table — orthogonal to the
        # sample. Tells us if backfill ever ran d1/d3 successfully.
        fill_stats = {}
        for col in ("close_d1", "close_d3", "close_d5", "close_d20"):
            n = db.query(sa_func.count(AnalysisOutcome.id)).filter(
                getattr(AnalysisOutcome, col).isnot(None)
            ).scalar() or 0
            fill_stats[col] = n

        return {
            "outcome_distinct_codes": len(outcome_codes),
            "kline_distinct_codes": len(kline_codes),
            "orphan_codes_count": len(orphan_codes),
            "orphan_codes": orphan_codes[:30],   # truncate for readability
            "covered_codes_count": len(covered_codes),
            "fill_stats": fill_stats,
            "sample_unscored": sample_detail,
        }
    finally:
        db.close()


@app.get("/api/_diag/klines-status")
def diag_klines_status():
    """Quick health check on the klines table — backfill_outcomes depends
    on it, so when scored_d5 stays at zero this is the first thing to
    inspect. Returns per-code coverage stats."""
    from sqlalchemy import func as sa_func
    from .db import SessionLocal
    from .models import Kline
    db = SessionLocal()
    try:
        total = db.query(sa_func.count(Kline.id)).scalar() or 0
        distinct_codes = db.query(
            sa_func.count(sa_func.distinct(Kline.code))
        ).scalar() or 0
        first = db.query(sa_func.min(Kline.date)).scalar()
        last = db.query(sa_func.max(Kline.date)).scalar()
        # Per-code row count distribution (min / median-ish / max)
        per_code = sorted([
            n for (code, n) in db.query(
                Kline.code, sa_func.count(Kline.id),
            ).group_by(Kline.code).all()
        ])
        median_per_code = per_code[len(per_code) // 2] if per_code else 0
        return {
            "total_rows": total,
            "distinct_codes": distinct_codes,
            "first_date": str(first) if first else None,
            "last_date": str(last) if last else None,
            "rows_per_code_min": per_code[0] if per_code else 0,
            "rows_per_code_median": median_per_code,
            "rows_per_code_max": per_code[-1] if per_code else 0,
            # If max != median, some codes are missing days — likely the
            # ones added to watchlist most recently. Backfill_outcomes
            # for those codes' anchors will keep returning "not enough
            # future bars yet" until kline_tick catches up.
        }
    finally:
        db.close()


@app.get("/api/_diag/outcomes-stats")
def diag_outcomes_stats():
    """Hit-rate summary grouped by prompt_version + actionable verdict.
    A 'hit' = 建议买入 with return_d5 > 0, or 建议卖出 with return_d5 < 0.
    6/10: each bucket also carries excess_return_d5 (vs same-day all-anchor
    median — strips market beta) and n_unique / hit_rate_dedup (last anchor
    per code per day — strips smart-cron clustering inflation).
    Public on purpose — no secrets, lets us watch quality without shell
    access."""
    from .services import outcomes as outcomes_svc
    return outcomes_svc.hit_rate_stats()


@app.get("/api/_diag/nd-outlook-stats")
def diag_nd_outlook_stats():
    """Score next_day_outlook.trend (看涨/看平/看跌) against actual next-day
    returns — the most falsifiable output of the product, tracked since
    6/10 (anchors before that have no nd_trend and are excluded). Grouped
    by trend and by the outlook's own 高/中/低 confidence. Expect ~1 day
    of lag before the first scored rows appear (needs close_d1)."""
    from .services import outcomes as outcomes_svc
    return outcomes_svc.nd_outlook_stats()


@app.post("/api/_diag/refresh-klines")
def diag_refresh_klines():
    """One-shot K-line bootstrap. Phase 9's _kline_tick fires at 16:30 BJT
    only — if the user wants technical面 data before the first scheduled
    tick (e.g., this Sunday before Monday open), POST here. Synchronous;
    ~1 minute for a 60-code watchlist."""
    from .services import kline as kline_svc
    return kline_svc.pull_for_watchlist()


@app.get("/api/_diag/snapshot-schema")
def diag_snapshot_schema():
    """Surface the actual snapshots table column list. Public on purpose —
    no secrets in column names, and AUTH_DISABLED is on anyway. Lets us
    verify migrations from outside without Railway shell access.

    Expected columns after the 4/27 schema bump: id, code, ts, price,
    change_pct, volume, turnover, main_net_flow, north_hold_change,
    signals, news, notices, lhb, pe_ratio, pb_ratio, turnover_rate,
    market_cap, circ_market_cap.
    """
    cols = snapshot_columns()
    expected_extras = ["pe_ratio", "pb_ratio", "turnover_rate", "market_cap", "circ_market_cap"]
    missing = [c for c in expected_extras if c not in cols]
    return {
        "dialect": engine.dialect.name,
        "columns": cols,
        "expected_extras": expected_extras,
        "missing_extras": missing,
        "ok": len(missing) == 0,
    }


@app.get("/api/_diag/outcomes-detail")
def diag_outcomes_detail():
    """Raw distribution of the analysis_outcomes table — diagnoses why
    hit_rate_stats is sparse. Breaks down total / scorable / actually-
    scored counts by actionable, plus distinct modes/prompt_versions and
    the time window of recorded anchors.

    Hit-rate-stats alone hides the fact that "scored" (return_d5 not null)
    might be 4% of total because either record_anchor is silently skipping
    most calls, or the daily backfill cron isn't keeping up. This endpoint
    shows the two layers separately."""
    from sqlalchemy import func as sa_func
    from .db import SessionLocal
    from .models import AnalysisOutcome
    db = SessionLocal()
    try:
        total = db.query(sa_func.count(AnalysisOutcome.id)).scalar() or 0
        scored = db.query(sa_func.count(AnalysisOutcome.id)).filter(
            AnalysisOutcome.return_d5.isnot(None)
        ).scalar() or 0

        # Group by actionable (total vs scored)
        by_action: dict[str, dict] = {}
        for actionable, n in db.query(
            AnalysisOutcome.actionable, sa_func.count(AnalysisOutcome.id),
        ).group_by(AnalysisOutcome.actionable).all():
            by_action[actionable] = {"total": n, "scored": 0}
        for actionable, n in db.query(
            AnalysisOutcome.actionable, sa_func.count(AnalysisOutcome.id),
        ).filter(AnalysisOutcome.return_d5.isnot(None)).group_by(
            AnalysisOutcome.actionable,
        ).all():
            by_action.setdefault(actionable, {"total": 0, "scored": 0})
            by_action[actionable]["scored"] = n

        # Distinct mode / prompt_version
        modes = sorted({m for (m,) in db.query(AnalysisOutcome.mode).distinct().all() if m})
        prompts = sorted({p for (p,) in db.query(
            AnalysisOutcome.prompt_version,
        ).distinct().all() if p})

        # Time window
        first = db.query(sa_func.min(AnalysisOutcome.generated_at)).scalar()
        last = db.query(sa_func.max(AnalysisOutcome.generated_at)).scalar()

        # NULL anchor_price guard — record_anchor() skips when price is
        # None, so this should always be zero. If it isn't, the schema
        # has nullable=True (which it shouldn't given record_anchor's
        # behavior) and we have phantom rows.
        null_anchor = db.query(sa_func.count(AnalysisOutcome.id)).filter(
            AnalysisOutcome.anchor_price.is_(None)
        ).scalar() or 0

        return {
            "total_anchors": total,
            "scored_d5": scored,
            "scored_pct": round(scored / total * 100, 1) if total else None,
            "by_actionable": by_action,
            "distinct_modes": modes,
            "distinct_prompt_versions": prompts,
            "first_anchor": first.isoformat() if first else None,
            "last_anchor": last.isoformat() if last else None,
            "null_anchor_price_count": null_anchor,
        }
    finally:
        db.close()


@app.post("/api/_diag/migrate-prompt-version")
def diag_migrate_prompt_version():
    """One-off retroactive fix for the pre-c231b60 hardcode bug.

    Before c231b60 every Analysis / AnalysisOutcome row got tagged
    prompt_version='v2.5-debate' regardless of whether it ran in single
    or debate mode — so a year of accumulated hit-rate data is filed
    under one bucket instead of two. This endpoint repairs the existing
    rows by splitting on the `mode` column:
      - prompt_version='v2.5-debate' AND mode='debate' → unchanged
      - prompt_version='v2.5-debate' AND mode!='debate' (or NULL) → 'v2.5-single'

    Idempotent and safe to re-run: the WHERE clause only catches the
    buggy tag, and post-fix rows already carry the correct value.
    Applies to both `analyses` and `analysis_outcomes` tables.
    """
    from sqlalchemy import text
    from .db import engine
    with engine.begin() as conn:
        # COALESCE handles rows where mode is NULL — default them to single
        # since that's the path that runs 99% of the time.
        analyses_n = conn.execute(text(
            "UPDATE analyses SET prompt_version = 'v2.5-' || COALESCE(NULLIF(mode, ''), 'single') "
            "WHERE prompt_version = 'v2.5-debate'"
        )).rowcount
        outcomes_n = conn.execute(text(
            "UPDATE analysis_outcomes SET prompt_version = 'v2.5-' || COALESCE(NULLIF(mode, ''), 'single') "
            "WHERE prompt_version = 'v2.5-debate'"
        )).rowcount
    return {
        "analyses_updated": analyses_n,
        "outcomes_updated": outcomes_n,
    }


@app.post("/api/_diag/migrate-confidence-to-int")
def diag_migrate_confidence_to_int():
    """One-off migration: key_table.confidence enum → integer.

    Pre-5/28 confidence was an enum "高"/"中"/"低" inside the key_table JSON.
    From now on it's a 0-100 integer (more granular for visual degradation
    + hit-rate correlation). To keep historical rows usable, we rewrite
    the JSON in place: 高→85, 中→65, 低→45 (rough midpoints of each
    bucket). Idempotent — the WHERE clause only catches enum strings, and
    new integer values are left alone.

    Postgres only (uses jsonb_set). Returns rows_updated.
    """
    from sqlalchemy import text
    from .db import engine
    if engine.dialect.name != "postgresql":
        return {"skipped": True, "reason": "non-postgres backend"}
    with engine.begin() as conn:
        # Note: key_table is stored as JSON (not JSONB), so we cast to
        # jsonb for jsonb_set then back to json for the column.
        # to_jsonb(integer) keeps all CASE branches returning the same
        # jsonb-numeric type (the earlier ::jsonb-on-strings + json
        # fallback variant errored with "CASE types jsonb and json cannot
        # be matched"). WHERE clause guarantees the CASE always matches
        # one of the three enums, so no ELSE branch is needed.
        rows_n = conn.execute(text(
            """
            UPDATE analyses
            SET key_table = jsonb_set(
                key_table::jsonb,
                '{confidence}',
                to_jsonb(
                    CASE key_table->>'confidence'
                        WHEN '高' THEN 85
                        WHEN '中' THEN 65
                        WHEN '低' THEN 45
                    END
                )
            )::json
            WHERE key_table->>'confidence' IN ('高', '中', '低')
            """
        )).rowcount
    return {"rows_updated": rows_n}


_regen_lock = __import__("threading").Lock()
_regen_state = {"v": False, "last_result": None, "last_started_at": None}


@app.post("/api/_diag/regenerate-all")
def diag_regenerate_all():
    """Admin one-shot: force re-analyze EVERY distinct watchlist code,
    bypassing both the stale/missing skip ladder AND the snapshot_id
    cache. Use when a new schema field shipped (e.g. valid_window) and
    you want every row to carry it before the next market open.

    ASYNC — distinct codes ~100 × ~5-7s/LLM call = ~10 min. Returns
    `{started: true}` immediately. Status at /regenerate-all/status.
    Re-entrant guard: a second call while one is running returns
    `{started: false, already_running: true}` instead of double-firing.

    Cost: at ~0.05 元/call, full pass = ~5 元. Run sparingly."""
    import threading
    from .services.cron import run_daily_analysis_job
    from datetime import datetime, timezone

    with _regen_lock:
        if _regen_state["v"]:
            return {"started": False, "already_running": True}
        _regen_state["v"] = True
        _regen_state["last_result"] = None
        _regen_state["last_started_at"] = datetime.now(timezone.utc).isoformat()

    def _worker():
        try:
            # force=True bypasses both skip ladder and snapshot_id cache.
            result = run_daily_analysis_job(
                only_stale=False, only_missing=False, force=True,
            )
            _regen_state["last_result"] = result
        except Exception as e:
            _regen_state["last_result"] = {"error": str(e)}
        finally:
            with _regen_lock:
                _regen_state["v"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True}


@app.get("/api/_diag/regenerate-all/status")
def diag_regenerate_all_status():
    """Status of the most recent /regenerate-all run."""
    return {
        "running": _regen_state["v"],
        "last_started_at": _regen_state["last_started_at"],
        "last_result": _regen_state["last_result"],
    }


@app.get("/api/_diag/hit-rate-by-confidence")
def diag_hit_rate_by_confidence():
    """Stratify hit_rate by the LLM's self-reported confidence bucket.

    Tests whether the confidence-as-int system (5/28) is actually
    discriminative — if "high" rows hit at clearly higher rate than
    "low" rows, the field is doing real work. If the rates are roughly
    equal, the LLM is throwing dice when picking numbers, and we need
    to redesign confidence scoring.

    Only buy/sell anchors (directional). Excludes anchors written
    before 5/29 (no confidence stored). May return small bucket sizes
    initially — give it 1-2 weeks of new anchors before treating it
    as conclusive.
    """
    from .services import outcomes as outcomes_svc
    return outcomes_svc.hit_rate_by_confidence()


@app.get("/api/_diag/smart-analyze-status")
def diag_smart_analyze_status():
    """Status of the smart intraday analysis tick (every 30 min in
    trading hours). Returns the last run's per-reason counters so we
    can see how often each trigger condition fires:

    - triggered: codes that passed _should_reanalyze
    - generated: actually called LLM (cache_hit shows how many were
      short-circuited by snapshot_id cache)
    - by_reason: distribution of skip / trigger reasons
        cooldown: existing analysis < 30 min old
        no_snap: snapshot not yet pulled
        no_existing: no prior analysis (daily 09:35 cron's job)
        no_change: same snapshot already analyzed
        price_move: |price - anchor| / anchor >= 1.5%
        signal_change: snap.signals differs from anchor
        stale: existing > 4h old (fallback)
        no_anchor: existing has no snapshot_id (legacy row)

    Useful for tuning thresholds — if "stale" dominates we need to
    relax price_move, if "cooldown" dominates we're triggering too
    often.
    """
    from .services.cron import _smart_state
    return {
        "running": _smart_state["running"],
        "last_started_at": _smart_state["last_started_at"],
        "last_result": _smart_state["last_result"],
    }


@app.get("/api/_diag/watchlist-stats")
def diag_watchlist_stats():
    """Watchlist 总体统计 — 估算 batch_analyze 类策略的成本规模。

    一次 LLM 调用 ~5-7 秒 + ~0.05 元 token。
    每日成本 = total × 跑次数 × 单价。比如:
      - total=200, 每小时跑1次, 盘中4小时 → 200 × 4 × 0.05 = 40 元/天
      - 增量过滤后只 10% 触发 → 4 元/天
    distinct_codes 远小于 total 说明用户们关注的票高度重叠 — 全市场
    级 batch 实际不用按 user 跑,只跑 distinct codes 即可。
    """
    from sqlalchemy import text
    from .db import engine
    with engine.begin() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM watchlist")).scalar() or 0
        distinct_codes = conn.execute(
            text("SELECT COUNT(DISTINCT code) FROM watchlist")
        ).scalar() or 0
        users_with = conn.execute(
            text("SELECT COUNT(DISTINCT user_id) FROM watchlist WHERE user_id IS NOT NULL")
        ).scalar() or 0
        # 每用户条数分布,看是否高度集中(少数重度用户) vs 分散
        per_user = conn.execute(text(
            "SELECT user_id, COUNT(*) AS n FROM watchlist "
            "WHERE user_id IS NOT NULL GROUP BY user_id ORDER BY n DESC"
        )).fetchall()
        counts = sorted([r[1] for r in per_user], reverse=True)
    distribution = {
        "max": counts[0] if counts else 0,
        "median": counts[len(counts) // 2] if counts else 0,
        "min": counts[-1] if counts else 0,
        "top_5_user_counts": counts[:5],
    }
    return {
        "total": total,
        "distinct_codes": distinct_codes,
        "users_with_watchlist": users_with,
        "avg_per_user": round(total / users_with, 1) if users_with > 0 else 0,
        "distribution": distribution,
    }


_shareholder_lock = __import__("threading").Lock()
_shareholder_running = {"v": False, "last_result": None}


@app.post("/api/_diag/refresh-shareholder")
def diag_refresh_shareholder():
    """Manual trigger: pull market-wide insider shareholding changes,
    filter to watchlist + 90 days, upsert into shareholder_changes. ASYNC
    — returns immediately, ~30s background work (single bulk akshare call
    然后 in-process filter)。

    Powers the analysis prompt's 股东变动 section. Re-running is safe
    (upserts by code+date+person+shares unique key)."""
    import threading
    from .services import shareholder as shareholder_svc

    with _shareholder_lock:
        if _shareholder_running["v"]:
            return {"started": False, "already_running": True}
        _shareholder_running["v"] = True
        _shareholder_running["last_result"] = None

    def _worker():
        try:
            result = shareholder_svc.pull_for_watchlist()
            _shareholder_running["last_result"] = result
        except Exception as e:
            _shareholder_running["last_result"] = {"error": str(e)}
        finally:
            with _shareholder_lock:
                _shareholder_running["v"] = False

    threading.Thread(target=_worker, daemon=True).start()
    return {"started": True}


@app.get("/api/_diag/refresh-shareholder/status")
def diag_refresh_shareholder_status():
    """Status of the most recent /refresh-shareholder run."""
    from .services import shareholder as shareholder_svc
    return {
        "running": _shareholder_running["v"],
        "progress": shareholder_svc.get_progress(),
        "last_result": _shareholder_running["last_result"],
    }


@app.get("/api/_diag/render-prompt")
def diag_render_prompt(code: str):
    """Render the actual _user_prompt for a stock and return it as plain
    text. Useful for verifying that newly-added sections (shareholder /
    peer comparison / market context) actually inject content vs returning
    '' due to fail-safe paths.

    Usage:
      curl "$BASE/api/_diag/render-prompt?code=688008"
    """
    from sqlalchemy import desc
    from sqlalchemy.orm import Session
    from .db import SessionLocal
    from .models import Snapshot, Watchlist
    from .services.analysis import _user_prompt, compute_data_completeness

    db: Session = SessionLocal()
    try:
        w = db.query(Watchlist).filter(Watchlist.code == code).first()
        if not w:
            return {"error": f"{code} not in watchlist"}
        s = (
            db.query(Snapshot)
            .filter(Snapshot.code == code)
            .order_by(desc(Snapshot.id))
            .first()
        )
        if s is None:
            return {"error": f"no snapshot for {code}"}

        data_comp = compute_data_completeness(s, code)
        prompt = _user_prompt(w, s, data_completeness=data_comp)

        # Section presence checks — quick visual indicator without scrolling
        # the full prompt.
        sections = {
            "shareholder": "## 内部人交易" in prompt,
            "peer": "## 同业可比" in prompt,
            "market": "## 大盘与板块表现" in prompt,
            "data_completeness": "## 输入数据状态" in prompt,
        }

        return {
            "code": code,
            "name": w.name,
            "data_completeness": data_comp,
            "prompt_length_chars": len(prompt),
            "sections_present": sections,
            "prompt": prompt,
        }
    finally:
        db.close()


@app.get("/api/_diag/akshare-shareholder-probe")
def diag_akshare_shareholder_probe(fn: str | None = None):
    """Phase 0 临时 endpoint:试 akshare 股东变动接口名 + 字段结构。

    调用方式:
      curl $BASE/api/_diag/akshare-shareholder-probe              # 列候选
      curl $BASE/api/_diag/akshare-shareholder-probe?fn=NAME      # 试单个接口

    单接口 5s 上限 (复用 scraper._safe_with_timeout),避免 Railway 30s
    edge timeout。Server 串行 try 15 次会撞 timeout,所以改成 query
    param 选单个 fn 跑。
    """
    try:
        import akshare as ak
    except Exception as e:
        return {"error": f"akshare import failed: {e}"}

    candidates = [
        "stock_share_change_cninfo",       # 巨潮资讯股本变动
        "stock_share_hold_change_szse",    # 深交所股东变动
        "stock_share_hold_change_bse",     # 北交所股东变动
        "stock_ggcg_em",                   # 东财高管增减持
        "stock_zh_a_gdhs",                 # 股东户数变化
    ]
    version = getattr(ak, "__version__", "unknown")

    # 不带 fn 参数 → 列候选 + 显示哪些函数在当前 akshare 版本里存在
    if fn is None:
        return {
            "akshare_version": version,
            "candidates": [
                {"fn": c, "exists": hasattr(ak, c)} for c in candidates
            ],
            "usage": "再次 curl 加 ?fn=NAME 试单个接口",
        }

    target = getattr(ak, fn, None)
    if target is None:
        return {"akshare_version": version, "fn": fn, "status": "not_in_akshare"}

    from .services.scraper import _safe_with_timeout
    test_code = "600519"
    attempts = [
        {"symbol": test_code},
        {"stock": test_code},
        {},
    ]
    out: dict = {"akshare_version": version, "fn": fn}
    for kw in attempts:
        df = _safe_with_timeout(target, _timeout=5.0, **kw)
        if df is None:
            out[str(kw)] = "timeout_or_error"
            continue
        try:
            shape = list(df.shape)
            cols = list(df.columns)[:25]
            sample = df.head(3).astype(str).to_dict(orient="records")
            out[str(kw)] = {"shape": shape, "columns": cols, "sample": sample}
            break  # 成功就停
        except Exception as e:
            out[str(kw)] = f"df_processing_error: {type(e).__name__}: {str(e)[:100]}"
    return out


@app.get("/health")
def health():
    return {"status": "ok"}
