"""盯盘 view + manual snapshot trigger."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

# 4h cache TTL for the LLM-generated analysis. Mirrors the value enforced
# inside services.analysis.get_cached(); kept here so list_stocks can compute
# is_fresh in a single SQL pass without re-querying per row.
ANALYSIS_FRESH_HOURS = 4

from ..auth import require_auth
from ..db import get_db
from ..models import Analysis, Snapshot, Watchlist
from ..services.analysis import generate as analysis_generate, get_cached as analysis_cached
from ..services.cron import run_daily_analysis_job, run_snapshot_job
from ..services.signals import has_strong
from ..services.users import resolve_owner

logger = logging.getLogger(__name__)

# Process-level guard so two simultaneous "手动抓取" clicks don't double-run
# the snapshot job. The lock + bool pair is intentionally simple — we only
# need correctness within a single backend replica (Railway runs 1 replica).
_snapshot_lock = threading.Lock()
_snapshot_running = False

# Same guard for the manual "批量解析" button — prevents the user from
# kicking off 23 LLM calls twice if they double-click.
_analysis_lock = threading.Lock()
_analysis_running = False

router = APIRouter(prefix="/api/stocks", tags=["stocks"], dependencies=[Depends(require_auth)])


def _user_watchlist(db: Session, owner: int | None):
    """Watchlist query scoped to the current request's owner. Mirrors
    routes.watchlist._scoped_query — when owner is None (legacy /
    AUTH_DISABLED with no admin set), returns the unscoped query so the
    pre-account-system behaviour still works."""
    q = db.query(Watchlist)
    if owner is not None:
        q = q.filter(Watchlist.user_id == owner)
    return q


class AnalysisBrief(BaseModel):
    """Just the bits the 盯盘 list needs from the cached Analysis row.

    Mirrors the v2 key_table schema (4/27): company_tag and red_flags are
    surfaced inline in the table so a row screams '维权 + 业绩塌方' without
    needing the user to drill into the detail page.
    """
    actionable: str            # 建议买入 / 观望 / 建议卖出 / 不建议入手
    one_line_reason: str
    company_tag: str           # one-line company portrait
    red_flags: list[str]       # hard-detected risk markers
    created_at: str
    is_fresh: bool             # < 4h old


class StockRow(BaseModel):
    code: str
    name: str
    exchange: str
    last_ts: str | None
    price: float | None
    change_pct: float | None
    main_net_flow: float | None
    signals: list[str]
    has_strong_signal: bool
    on_lhb: bool
    starred: bool                     # user-marked "特别关注"
    analysis: AnalysisBrief | None  # null when never generated


@router.get("", response_model=list[StockRow])
def list_stocks(
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Latest snapshot per watched code, joined with watchlist + cached
    analysis. Watchlist is scoped to the authenticated user; snapshots and
    analyses are shared market-level state so we just join through."""
    owner = resolve_owner(user_id, db)
    watch = {w.code: w for w in _user_watchlist(db, owner).all()}
    if not watch:
        return []

    # Latest-per-code: subquery for max(id) per code, then join.
    subq = (
        db.query(Snapshot.code, Snapshot.id.label("id"))
        .order_by(Snapshot.code, desc(Snapshot.id))
        .distinct(Snapshot.code)
        .subquery()
    )
    # The DISTINCT ON above is Postgres-only. Fallback for SQLite (smoke tests):
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        latest_ids: list[int] = []
        for code in watch.keys():
            r = (
                db.query(Snapshot.id)
                .filter(Snapshot.code == code)
                .order_by(desc(Snapshot.id))
                .first()
            )
            if r:
                latest_ids.append(r[0])
        snaps = db.query(Snapshot).filter(Snapshot.id.in_(latest_ids)).all() if latest_ids else []
    else:
        snaps = db.query(Snapshot).join(subq, Snapshot.id == subq.c.id).all()

    by_code = {s.code: s for s in snaps}

    # Junk-row fallback: if the latest snapshot for a code has neither a
    # price nor a main_net_flow (e.g., a 16:00 hourly job ran with all
    # akshare endpoints failing and wrote a row of nulls), surface the
    # most recent row that *does* have data so the 盯盘 list keeps
    # showing yesterday's close instead of going blank.
    for code in list(by_code.keys()):
        s = by_code[code]
        if s.price is None and s.main_net_flow is None:
            good = (
                db.query(Snapshot)
                .filter(
                    Snapshot.code == code,
                    (Snapshot.price.isnot(None)) | (Snapshot.main_net_flow.isnot(None)),
                )
                .order_by(desc(Snapshot.id))
                .first()
            )
            if good is not None:
                by_code[code] = good

    # One-shot pull of every analysis row for the watched codes.
    analyses = {
        a.code: a
        for a in db.query(Analysis).filter(Analysis.code.in_(list(watch.keys()))).all()
    }
    fresh_cutoff = datetime.now(timezone.utc) - timedelta(hours=ANALYSIS_FRESH_HOURS)

    def _brief(code: str) -> AnalysisBrief | None:
        a = analyses.get(code)
        if a is None:
            return None
        kt = a.key_table or {}
        # Schema v2 invalidation: rows from the old schema (no company_tag)
        # are treated as missing so batch_analysis re-generates them with
        # the new structure. No DB cleanup needed.
        if "company_tag" not in kt:
            return None
        created = a.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return AnalysisBrief(
            actionable=str(kt.get("actionable") or ""),
            one_line_reason=str(kt.get("one_line_reason") or ""),
            company_tag=str(kt.get("company_tag") or ""),
            red_flags=list(kt.get("red_flags") or []),
            created_at=created.isoformat() if created else "",
            is_fresh=bool(created and created >= fresh_cutoff),
        )

    rows: list[StockRow] = []
    for code, w in watch.items():
        s = by_code.get(code)
        signals = (s.signals if s else None) or []
        rows.append(StockRow(
            code=code,
            name=w.name,
            exchange=w.exchange,
            last_ts=s.ts.isoformat() if s else None,
            price=(s.price if s else None),
            change_pct=(s.change_pct if s else None),
            main_net_flow=(s.main_net_flow if s else None),
            signals=signals,
            has_strong_signal=has_strong(signals),
            on_lhb=bool(s.lhb) if s else False,
            starred=bool(getattr(w, "starred", False)),
            analysis=_brief(code),
        ))
    # Order key, most-important first:
    #   1. Starred rows (user said "watch this closely") — float to top of
    #      whatever bucket the frontend groups them into
    #   2. Strong-signal rows (limit_up/down, big notice, lhb)
    #   3. Larger |change_pct| comes first
    rows.sort(key=lambda r: (
        not r.starred,
        not r.has_strong_signal,
        -abs(r.change_pct or 0),
    ))
    return rows


class SnapshotTriggerResult(BaseModel):
    started: bool
    already_running: bool = False


class SnapshotStatus(BaseModel):
    running: bool


def _run_snapshot_in_background(post_close: bool) -> None:
    global _snapshot_running
    try:
        run_snapshot_job(post_close=post_close)
    except Exception:
        logger.exception("background snapshot job failed")
    finally:
        with _snapshot_lock:
            _snapshot_running = False


@router.post("/snapshot", response_model=SnapshotTriggerResult)
def trigger_snapshot(post_close: bool = False):
    """Kick off the snapshot job in a background thread and return immediately.

    The job collects 4 akshare endpoints per code; for 20+ codes it routinely
    takes 30–90s. Returning sync would let the browser/edge proxy time out
    even though the job itself succeeds. The frontend polls `/api/stocks`
    while we run.
    """
    global _snapshot_running
    with _snapshot_lock:
        if _snapshot_running:
            return SnapshotTriggerResult(started=False, already_running=True)
        _snapshot_running = True

    threading.Thread(
        target=_run_snapshot_in_background,
        args=(post_close,),
        daemon=True,
        name="snapshot-job",
    ).start()
    return SnapshotTriggerResult(started=True)


@router.get("/snapshot/status", response_model=SnapshotStatus)
def snapshot_status():
    """Lets the frontend poll whether a manual/background job is still running."""
    return SnapshotStatus(running=_snapshot_running)


# --- Batch LLM analysis ---------------------------------------------------
# These two routes are intentionally above /{code} below so the static
# /analysis/batch path doesn't get parsed as code="analysis".


class AnalysisBatchResult(BaseModel):
    started: bool
    already_running: bool = False


class AnalysisBatchStatus(BaseModel):
    running: bool


def _run_analysis_batch_in_background(only_missing: bool):
    global _analysis_running
    try:
        # only_missing=True: fill in 待生成 only (skip any v2 cached row).
        # only_missing=False: force regenerate every code.
        run_daily_analysis_job(only_stale=False, only_missing=only_missing)
    except Exception:
        logger.exception("batch analysis job failed")
    finally:
        with _analysis_lock:
            _analysis_running = False


@router.post("/analysis/batch", response_model=AnalysisBatchResult)
def trigger_batch_analysis(only_missing: bool = True):
    """Generate LLM analyses for the watchlist.

    Default `only_missing=true` matches the 盯盘 button's "fill 待生成 only"
    behavior — clicking when 0 are pending should pass `only_missing=false`
    explicitly to force-regen every code (the frontend confirms first).

    Fire-and-forget: launches a daemon thread, returns immediately. The
    frontend polls /analysis/batch/status + /api/stocks to follow progress
    (rows light up with their actionable verdict as each LLM call lands).
    """
    global _analysis_running
    with _analysis_lock:
        if _analysis_running:
            return AnalysisBatchResult(started=False, already_running=True)
        _analysis_running = True

    threading.Thread(
        target=_run_analysis_batch_in_background,
        kwargs={"only_missing": only_missing},
        daemon=True,
        name="batch-analysis",
    ).start()
    return AnalysisBatchResult(started=True)


@router.get("/analysis/batch/status", response_model=AnalysisBatchStatus)
def batch_analysis_status():
    return AnalysisBatchStatus(running=_analysis_running)


class StockDetail(BaseModel):
    code: str
    name: str
    exchange: str
    last_ts: str | None
    price: float | None
    change_pct: float | None
    main_net_flow: float | None
    signals: list[str]
    news: list[dict[str, Any]]
    notices: list[dict[str, Any]]
    lhb: dict[str, Any] | None


@router.get("/{code}", response_model=StockDetail)
def stock_detail(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Latest snapshot detail for one stock. 404 if the code isn't in the
    current user's watchlist — keeps user A from poking at codes only user
    B watches just because they happen to share market data."""
    owner = resolve_owner(user_id, db)
    w = _user_watchlist(db, owner).filter(Watchlist.code == code).first()
    if not w:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")
    s = (
        db.query(Snapshot)
        .filter(Snapshot.code == code)
        .order_by(desc(Snapshot.id))
        .first()
    )
    # Same junk-row fallback as list_stocks: if the latest snapshot is
    # all-null on price+flow, walk back to the most recent good one.
    if s is not None and s.price is None and s.main_net_flow is None:
        good = (
            db.query(Snapshot)
            .filter(
                Snapshot.code == code,
                (Snapshot.price.isnot(None)) | (Snapshot.main_net_flow.isnot(None)),
            )
            .order_by(desc(Snapshot.id))
            .first()
        )
        if good is not None:
            s = good
    return StockDetail(
        code=code,
        name=w.name,
        exchange=w.exchange,
        last_ts=s.ts.isoformat() if s else None,
        price=(s.price if s else None),
        change_pct=(s.change_pct if s else None),
        main_net_flow=(s.main_net_flow if s else None),
        signals=(s.signals if s else None) or [],
        news=(s.news if s else None) or [],
        notices=(s.notices if s else None) or [],
        lhb=(s.lhb if s else None),
    )


# --- Phase 3: deep analysis ----------------------------------------------


class AnalysisOut(BaseModel):
    code: str
    key_table: dict[str, Any]
    deep_analysis: str
    model: str
    strategy: str
    created_at: str
    snapshot_id: int | None
    is_fresh: bool

    @classmethod
    def from_row(cls, row: Analysis, is_fresh: bool) -> "AnalysisOut":
        return cls(
            code=row.code,
            key_table=row.key_table,
            deep_analysis=row.deep_analysis,
            model=row.model,
            strategy=row.strategy,
            created_at=row.created_at.isoformat() if row.created_at else "",
            snapshot_id=row.snapshot_id,
            is_fresh=is_fresh,
        )


@router.get("/{code}/analysis", response_model=AnalysisOut | None)
def get_analysis(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Return cached analysis (shared across users — see plan: "Analysis
    缓存：全局共享") if the code is in the current user's watchlist. Codes
    they don't watch get a 404 to avoid leaking which stocks others care
    about."""
    owner = resolve_owner(user_id, db)
    if not _user_watchlist(db, owner).filter(Watchlist.code == code).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")
    row = db.query(Analysis).filter(Analysis.code == code).first()
    if row is None:
        return None
    fresh = analysis_cached(db, code) is not None
    return AnalysisOut.from_row(row, is_fresh=fresh)


@router.post("/{code}/analysis", response_model=AnalysisOut)
def generate_analysis(
    code: str,
    db: Session = Depends(get_db),
    user_id: int | None = Depends(require_auth),
):
    """Force regenerate. Scoped to the user's watchlist (404 if not theirs)
    so users can't burn LLM tokens for codes they don't follow."""
    owner = resolve_owner(user_id, db)
    if not _user_watchlist(db, owner).filter(Watchlist.code == code).first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not in watchlist")
    try:
        row = analysis_generate(db, code)
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return AnalysisOut.from_row(row, is_fresh=True)
