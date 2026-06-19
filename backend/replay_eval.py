"""Replay-based LLM evaluation harness.

Goal: compare candidate models (火山 ARK coding plan) against the current
kimi-k2.5 baseline on the *same* historical inputs, scored against the
*same* known forward d5 returns. Gives A/B-grade rigor without a 4-week
wall clock by replaying instead of waiting.

Why replay > live A/B for this decision:
  - 300 samples × 6 models × (1 single + 3 debate calls) ≈ 7200 calls,
    runnable overnight.
  - Same snapshot inputs → controls for "the market moved" confound.
  - Forward returns are already in the klines table — zero waiting to
    compute hit_rate_dedup + excess_return_d5.

What is NOT controlled (be honest with yourself reading scoreboard.md):
  - Production peers / shareholder / CNINFO main-business data flow into
    _user_prompt at TODAY's state, not at snapshot-time state. For samples
    < 60 days old this drifts ≤ 1 financial period and the K-line / news
    are point-in-time. Acceptable for ranking; not airtight for absolute
    levels.

Outputs land in backend/eval_out/:
  samples.jsonl       — sampled anchor list (stratified by actionable)
  prompts.jsonl       — rendered user_prompt + system_prompt per sample
  runs/<m>_<mode>.jsonl  — one line per LLM call, JSON Lines, append-only
                           (lets us resume after a crash)
  scoreboard.md       — model × mode metrics, ranked
  disagreements.md    — cases where candidate verdict ≠ baseline verdict

Subcommands (all idempotent; each one re-reads its inputs from disk):
  samples   — sample 300 anchors from analysis_outcomes
  prompts   — render point-in-time user_prompts (monkey-patches kline/fin
              lookups during the call, single-threaded)
  run       — for each (model, mode, sample), call LLM if not already done
  score     — score everything in runs/, write scoreboard + disagreements

  all       — run everything in order; skips already-done work
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable

from anthropic import Anthropic
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import AnalysisOutcome, Financial, Kline, Snapshot, Watchlist
from app.services.analysis import (
    ANALYSIS_TOOL,
    _system_prompt,
    _user_prompt,
    DEFAULT_MODEL as BASELINE_MODEL,
)
from app.services.analysis_debate import (
    BULL_TOOL, BEAR_TOOL,
    _bull_system_prompt, _bear_system_prompt,
    render_debate_for_judge, judge_system_prompt_suffix,
)
from app.services.strategy import get as get_strategy

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("replay_eval")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Model registry
# --------------------------------------------------------------------------

@dataclass
class ModelSpec:
    """How to reach one candidate. We deliberately keep base_url and api_key
    explicit per spec — baseline kimi uses ANTHROPIC_* (dashscope), candidates
    use VOLCENGINE_* (ARK), no shared global state."""
    name: str               # the slug we use in filenames + the scoreboard
    api_model: str          # what the gateway expects
    base_url: str
    api_key: str
    notes: str = ""

    @property
    def safe_name(self) -> str:
        return self.name.replace("/", "_").replace(":", "_")


def build_default_models() -> list[ModelSpec]:
    """5 candidates from 火山 ARK + 1 baseline. Edit this to add/remove."""
    ark_url = (settings.VOLCENGINE_BASE_URL or
               "https://ark.cn-beijing.volces.com/api/coding")
    ark_key = settings.VOLCENGINE_API_KEY
    return [
        # baseline — current production model on dashscope
        ModelSpec(
            name="kimi-k2.5",
            api_model=settings.ANALYSIS_MODEL or BASELINE_MODEL,
            base_url=settings.ANTHROPIC_BASE_URL,
            api_key=settings.ANTHROPIC_API_KEY,
            notes="baseline (current production)",
        ),
        # candidates on 火山 ARK
        ModelSpec("kimi-k2.6",            "kimi-k2.6",            ark_url, ark_key,
                  "direct successor of baseline kimi"),
        ModelSpec("deepseek-v4-pro",      "deepseek-v4-pro",      ark_url, ark_key,
                  "DeepSeek 旗舰非 code"),
        ModelSpec("glm-5.2",              "glm-5.2",              ark_url, ark_key,
                  "智谱旗舰, 1M context"),
        ModelSpec("doubao-seed-2.0-pro",  "doubao-seed-2.0-pro",  ark_url, ark_key,
                  "豆包旗舰, 中文场景"),
        ModelSpec("minimax-m3",           "minimax-m3",           ark_url, ark_key,
                  "minimax 新旗舰"),
    ]


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

OUT_DIR = Path(__file__).resolve().parent / "eval_out"
SAMPLES_FILE = OUT_DIR / "samples.jsonl"
PROMPTS_FILE = OUT_DIR / "prompts.jsonl"
RUNS_DIR = OUT_DIR / "runs"
SCOREBOARD_MD = OUT_DIR / "scoreboard.md"
DISAGREEMENTS_MD = OUT_DIR / "disagreements.md"


# --------------------------------------------------------------------------
# Stage 1: sample anchors
# --------------------------------------------------------------------------

# Target buckets — informed by the production verdict distribution. Tune
# if any bucket runs short (we log + fall back to "take what's available").
TARGET_PER_BUCKET = {
    "建议买入": 80,
    "建议卖出": 80,
    "观望":     80,
    "不建议入手": 60,
}


def stage_samples(n_target: int, max_age_days: int, seed: int) -> None:
    """Pull stratified anchors with return_d5 in [now - max_age_days, now).

    Writes one line per sample to samples.jsonl:
      {sample_id, code, snapshot_id, generated_at, actionable, anchor_price,
       return_d5, model_at_anchor}

    The samples are tied to snapshot_id (not the full Snapshot row content)
    so the renderer can re-load fresh from DB each time — keeps the JSONL
    light and lets us swap snapshots in tests.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(AnalysisOutcome)
            .filter(
                AnalysisOutcome.return_d5.isnot(None),
                AnalysisOutcome.generated_at >= cutoff,
                AnalysisOutcome.anchor_price.isnot(None),
            )
            .all()
        )
    finally:
        db.close()

    by_verdict: dict[str, list[AnalysisOutcome]] = {}
    for o in rows:
        by_verdict.setdefault(o.actionable or "?", []).append(o)

    logger.info("eligible anchors (last %d days, return_d5 filled): %d",
                max_age_days, len(rows))
    for v, lst in by_verdict.items():
        logger.info("  %s: %d", v, len(lst))

    # Stratified sampling. If a bucket is short, take everything in it.
    target_total = n_target if n_target else sum(TARGET_PER_BUCKET.values())
    scale = target_total / sum(TARGET_PER_BUCKET.values())
    targets = {v: max(1, int(round(t * scale))) for v, t in TARGET_PER_BUCKET.items()}

    picked: list[AnalysisOutcome] = []
    for verdict, target in targets.items():
        pool = by_verdict.get(verdict, [])
        if not pool:
            logger.warning("bucket %s has 0 eligible anchors — skipping", verdict)
            continue
        k = min(target, len(pool))
        if k < target:
            logger.warning("bucket %s short: wanted %d got %d", verdict, target, k)
        picked.extend(rng.sample(pool, k))

    rng.shuffle(picked)

    with SAMPLES_FILE.open("w") as f:
        for i, o in enumerate(picked):
            ga = o.generated_at
            if ga.tzinfo is None:
                ga = ga.replace(tzinfo=timezone.utc)
            f.write(json.dumps({
                "sample_id": i,
                "code": o.code,
                "snapshot_id": _resolve_snapshot_id(o),
                "generated_at": ga.isoformat(),
                "actionable": o.actionable,
                "anchor_price": o.anchor_price,
                "return_d5": o.return_d5,
                "return_d1": o.return_d1,
                "model_at_anchor": o.model,
            }, ensure_ascii=False) + "\n")
    logger.info("wrote %d samples → %s", len(picked), SAMPLES_FILE)


def _resolve_snapshot_id(o: AnalysisOutcome) -> int | None:
    """Find the snapshot row that this outcome was anchored on. The
    AnalysisOutcome doesn't carry snapshot_id directly — we look up the
    closest snapshot row for (code, generated_at)."""
    db: Session = SessionLocal()
    try:
        ga = o.generated_at
        if ga.tzinfo is None:
            ga = ga.replace(tzinfo=timezone.utc)
        # Snapshot at or just before generated_at. Outcomes are written
        # right after Analysis persists so the matching snapshot.ts ≈
        # generated_at to within seconds.
        snap = (
            db.query(Snapshot)
            .filter(Snapshot.code == o.code, Snapshot.ts <= ga + timedelta(minutes=5))
            .order_by(Snapshot.ts.desc())
            .first()
        )
        return snap.id if snap else None
    finally:
        db.close()


# --------------------------------------------------------------------------
# Stage 2: render prompts (point-in-time)
# --------------------------------------------------------------------------

@contextmanager
def _patched_pit(as_of_date_str: str):
    """Monkey-patch kline/financials lookups to honor a point-in-time cutoff.

    Used while rendering prompts for a single sample. SERIAL only — the
    patches are module-level globals, concurrent renders would clobber
    each other. We render serially upfront anyway (fast), parallel only
    on the LLM-call stage.

    `as_of_date_str` is YYYY-MM-DD (snapshot.ts.date()).
    """
    from app.services import kline as kline_svc
    from app.services import financials as fin_svc

    # Saved originals so we can restore on exit
    orig = {
        "kline_latest": kline_svc.latest_for_code,
        "kline_recent": kline_svc.recent_for_code,
        "fin_latest":   fin_svc.latest_for_code,
    }

    def kline_latest_pit(code: str):
        db: Session = SessionLocal()
        try:
            return (
                db.query(Kline)
                .filter(Kline.code == code, Kline.date <= as_of_date_str)
                .order_by(Kline.date.desc())
                .first()
            )
        finally:
            db.close()

    def kline_recent_pit(code: str, days: int = 20):
        db: Session = SessionLocal()
        try:
            rows = (
                db.query(Kline)
                .filter(Kline.code == code, Kline.date <= as_of_date_str)
                .order_by(Kline.date.desc())
                .limit(days)
                .all()
            )
            rows.reverse()
            return rows
        finally:
            db.close()

    # Financials publication lag: companies file ~30 days after period end.
    # report_date is YYYYMMDD; compare lexicographically after both are 8-char.
    fin_cutoff = (
        datetime.strptime(as_of_date_str, "%Y-%m-%d").date() - timedelta(days=30)
    ).strftime("%Y%m%d")

    def fin_latest_pit(code: str, n: int = 2):
        db: Session = SessionLocal()
        try:
            return (
                db.query(Financial)
                .filter(Financial.code == code, Financial.report_date <= fin_cutoff)
                .order_by(Financial.report_date.desc())
                .limit(n)
                .all()
            )
        finally:
            db.close()

    kline_svc.latest_for_code = kline_latest_pit
    kline_svc.recent_for_code = kline_recent_pit
    fin_svc.latest_for_code = fin_latest_pit
    try:
        yield
    finally:
        kline_svc.latest_for_code = orig["kline_latest"]
        kline_svc.recent_for_code = orig["kline_recent"]
        fin_svc.latest_for_code = orig["fin_latest"]


def stage_prompts() -> None:
    """For each sample, render the user_prompt + system_prompt under
    point-in-time kline/financial cutoffs. Writes prompts.jsonl."""
    if not SAMPLES_FILE.exists():
        logger.error("samples file missing — run `samples` first"); sys.exit(2)

    strat = get_strategy(None)
    system_blocks = _system_prompt(strat)

    out_lines = []
    skipped_no_snap = 0
    with SAMPLES_FILE.open() as f:
        samples = [json.loads(line) for line in f]

    for sample in samples:
        if sample["snapshot_id"] is None:
            skipped_no_snap += 1
            continue
        db: Session = SessionLocal()
        try:
            snap: Snapshot | None = db.query(Snapshot).filter(
                Snapshot.id == sample["snapshot_id"]).first()
            w: Watchlist | None = db.query(Watchlist).filter(
                Watchlist.code == sample["code"]).first()
        finally:
            db.close()

        if snap is None:
            skipped_no_snap += 1
            continue
        # Synthetic watchlist if user_id-bound row missing — _user_prompt only
        # reads w.code/name/exchange.
        if w is None:
            w = Watchlist(code=sample["code"], name=sample["code"],
                          exchange=_infer_exchange(sample["code"]),
                          user_id=None)

        as_of = snap.ts.date().isoformat()
        try:
            with _patched_pit(as_of):
                user_msg = _user_prompt(w, snap, data_completeness=None)
        except Exception as e:
            logger.warning("render failed for sample %d (%s): %s",
                           sample["sample_id"], sample["code"], e)
            continue

        out_lines.append({
            "sample_id": sample["sample_id"],
            "code": sample["code"],
            "as_of": as_of,
            "system": _flatten_system(system_blocks),
            "user": user_msg,
        })

    PROMPTS_FILE.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in out_lines))
    logger.info("rendered %d prompts → %s (skipped %d: no snapshot)",
                len(out_lines), PROMPTS_FILE, skipped_no_snap)


def _flatten_system(blocks: list[dict[str, Any]]) -> str:
    return "\n\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))


def _infer_exchange(code: str) -> str:
    if code.startswith(("60", "68")): return "sh"
    if code.startswith(("00", "30")): return "sz"
    if code.startswith(("8", "4", "92")): return "bj"
    return "unknown"


# --------------------------------------------------------------------------
# Stage 3: run LLM
# --------------------------------------------------------------------------

@dataclass
class RunResult:
    sample_id: int
    code: str
    model: str
    mode: str               # "single" | "debate"
    schema_ok: bool
    actionable: str | None = None
    confidence: int | float | None = None
    buy_price_low: float | None = None
    buy_price_high: float | None = None
    sell_price_low: float | None = None
    sell_price_high: float | None = None
    valid_window: str | None = None
    red_flags: list[str] = field(default_factory=list)
    next_day_trend: str | None = None
    next_day_confidence: int | float | None = None
    wall_time_s: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None
    raw_payload: dict[str, Any] | None = None


def stage_run(models: list[str] | None, modes: list[str], workers: int,
              max_samples: int | None) -> None:
    if not PROMPTS_FILE.exists():
        logger.error("prompts file missing — run `prompts` first"); sys.exit(2)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    all_specs = {m.name: m for m in build_default_models()}
    selected = [all_specs[n] for n in (models or list(all_specs))]

    with PROMPTS_FILE.open() as f:
        prompts = [json.loads(line) for line in f]
    if max_samples:
        prompts = prompts[:max_samples]

    for spec in selected:
        if not spec.api_key:
            logger.warning("%s: no api_key configured, skipping", spec.name); continue
        for mode in modes:
            run_file = RUNS_DIR / f"{spec.safe_name}_{mode}.jsonl"
            done = _already_done(run_file)
            todo = [p for p in prompts if p["sample_id"] not in done]
            logger.info("%s × %s: %d total, %d done, %d todo",
                        spec.name, mode, len(prompts), len(done), len(todo))
            if not todo:
                continue
            _dispatch(spec, mode, todo, run_file, workers)


def _already_done(run_file: Path) -> set[int]:
    if not run_file.exists():
        return set()
    done = set()
    with run_file.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                done.add(r["sample_id"])
            except Exception:
                pass
    return done


def _dispatch(spec: ModelSpec, mode: str, prompts: list[dict],
              run_file: Path, workers: int) -> None:
    """Fan out prompts across `workers` threads; append each result to JSONL
    as soon as it lands so a crash mid-batch loses at most one in-flight call."""
    client = Anthropic(
        api_key=spec.api_key,
        base_url=spec.base_url or None,
        timeout=180.0,
        max_retries=0,
    )
    write_lock = Lock()
    fh = run_file.open("a")

    def _one(p):
        try:
            if mode == "single":
                result = _call_single(client, spec, p)
            else:
                result = _call_debate(client, spec, p)
        except Exception as e:
            result = RunResult(
                sample_id=p["sample_id"], code=p["code"],
                model=spec.name, mode=mode, schema_ok=False,
                error=f"{type(e).__name__}: {str(e)[:200]}",
            )
        with write_lock:
            fh.write(json.dumps(asdict(result), ensure_ascii=False) + "\n")
            fh.flush()
        return result

    completed = 0
    t0 = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, p) for p in prompts]
            for fut in as_completed(futures):
                completed += 1
                if completed % 10 == 0 or completed == len(prompts):
                    elapsed = time.monotonic() - t0
                    rate = completed / max(elapsed, 1e-6)
                    eta = (len(prompts) - completed) / max(rate, 1e-6)
                    logger.info("%s × %s progress: %d/%d  rate=%.2f/s  eta=%.0fs",
                                spec.name, mode, completed, len(prompts), rate, eta)
    finally:
        fh.close()


def _call_single(client: Anthropic, spec: ModelSpec,
                 prompt: dict) -> RunResult:
    """One LLM call with the production submit_analysis tool. Falls back from
    forced tool_choice to 'any' on a 400 — same pattern as analysis.generate."""
    t0 = time.monotonic()
    base = {
        "model": spec.api_model,
        "max_tokens": 8192,
        "system": prompt["system"],
        "tools": [ANALYSIS_TOOL],
        "messages": [{"role": "user", "content": prompt["user"]}],
    }
    try:
        msg = client.messages.create(
            **base, tool_choice={"type": "tool", "name": "submit_analysis"},
        )
    except Exception as e:
        if "tool_choice" in str(e) or "400" in str(e):
            msg = client.messages.create(**base, tool_choice={"type": "any"})
        else:
            raise
    wall = time.monotonic() - t0

    tool_use = next((b for b in msg.content
                     if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        return RunResult(
            sample_id=prompt["sample_id"], code=prompt["code"],
            model=spec.name, mode="single", schema_ok=False,
            wall_time_s=wall, error="no tool_use block",
            input_tokens=getattr(getattr(msg, "usage", None), "input_tokens", None),
            output_tokens=getattr(getattr(msg, "usage", None), "output_tokens", None),
        )
    payload = tool_use.input  # type: ignore[assignment]
    return _extract_result(prompt, spec, "single", payload, wall, msg)


def _call_debate(client: Anthropic, spec: ModelSpec,
                 prompt: dict) -> RunResult:
    """bull → bear → judge, all on the same model. 3 calls total. Token
    + wall_time fields aggregate across the three turns."""
    t0 = time.monotonic()
    in_tok = 0
    out_tok = 0

    # Bull turn
    bull, msg_bull = _role_call(client, spec.api_model, prompt["user"],
                                 _bull_system_prompt(), BULL_TOOL,
                                 "submit_bull_view")
    in_tok += getattr(getattr(msg_bull, "usage", None), "input_tokens", 0) or 0
    out_tok += getattr(getattr(msg_bull, "usage", None), "output_tokens", 0) or 0

    # Bear turn
    bear, msg_bear = _role_call(client, spec.api_model, prompt["user"],
                                 _bear_system_prompt(), BEAR_TOOL,
                                 "submit_bear_view")
    in_tok += getattr(getattr(msg_bear, "usage", None), "input_tokens", 0) or 0
    out_tok += getattr(getattr(msg_bear, "usage", None), "output_tokens", 0) or 0

    # Judge turn: judge sees the same system prompt as single + the debate
    # suffix, and the user message is base_user + bull/bear render.
    judge_user = prompt["user"] + render_debate_for_judge(bull, bear)
    judge_system = prompt["system"] + "\n\n" + judge_system_prompt_suffix()
    base = {
        "model": spec.api_model,
        "max_tokens": 8192,
        "system": judge_system,
        "tools": [ANALYSIS_TOOL],
        "messages": [{"role": "user", "content": judge_user}],
    }
    try:
        msg_judge = client.messages.create(
            **base, tool_choice={"type": "tool", "name": "submit_analysis"},
        )
    except Exception as e:
        if "tool_choice" in str(e) or "400" in str(e):
            msg_judge = client.messages.create(**base, tool_choice={"type": "any"})
        else:
            raise
    in_tok += getattr(getattr(msg_judge, "usage", None), "input_tokens", 0) or 0
    out_tok += getattr(getattr(msg_judge, "usage", None), "output_tokens", 0) or 0
    wall = time.monotonic() - t0

    tool_use = next((b for b in msg_judge.content
                     if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        return RunResult(
            sample_id=prompt["sample_id"], code=prompt["code"],
            model=spec.name, mode="debate", schema_ok=False, wall_time_s=wall,
            input_tokens=in_tok, output_tokens=out_tok,
            error="judge returned no tool_use",
        )
    res = _extract_result(prompt, spec, "debate", tool_use.input, wall, None)
    res.input_tokens = in_tok
    res.output_tokens = out_tok
    return res


def _role_call(client: Anthropic, model: str, user_msg: str,
               sys_prompt: str, tool: dict, tool_name: str):
    base = {
        "model": model,
        "max_tokens": 4096,
        "system": sys_prompt,
        "tools": [tool],
        "messages": [{"role": "user", "content": user_msg}],
    }
    try:
        msg = client.messages.create(**base,
            tool_choice={"type": "tool", "name": tool_name})
    except Exception as e:
        if "tool_choice" in str(e) or "400" in str(e):
            msg = client.messages.create(**base, tool_choice={"type": "any"})
        else:
            raise
    tool_use = next((b for b in msg.content
                     if getattr(b, "type", None) == "tool_use"), None)
    if tool_use is None:
        raise RuntimeError(f"no tool_use for {tool_name}")
    return tool_use.input, msg


def _extract_result(prompt: dict, spec: ModelSpec, mode: str,
                    payload: dict, wall: float, msg) -> RunResult:
    payload.pop("analysis_thinking", None)  # CoT scratchpad
    kt = payload.get("key_table") or {}
    nd = kt.get("next_day_outlook") or {}

    in_tok = getattr(getattr(msg, "usage", None), "input_tokens", None) if msg else None
    out_tok = getattr(getattr(msg, "usage", None), "output_tokens", None) if msg else None

    return RunResult(
        sample_id=prompt["sample_id"], code=prompt["code"],
        model=spec.name, mode=mode,
        schema_ok=True,
        actionable=kt.get("actionable"),
        confidence=kt.get("confidence"),
        buy_price_low=kt.get("buy_price_low"),
        buy_price_high=kt.get("buy_price_high"),
        sell_price_low=kt.get("sell_price_low"),
        sell_price_high=kt.get("sell_price_high"),
        valid_window=kt.get("valid_window"),
        red_flags=kt.get("red_flags") or [],
        next_day_trend=nd.get("trend"),
        next_day_confidence=nd.get("confidence"),
        wall_time_s=wall,
        input_tokens=in_tok,
        output_tokens=out_tok,
        raw_payload=payload,
    )


# --------------------------------------------------------------------------
# Stage 4: score
# --------------------------------------------------------------------------

def stage_score() -> None:
    """Aggregate everything in runs/ into a scoreboard + disagreement list."""
    if not RUNS_DIR.exists():
        logger.error("runs/ dir missing — run `run` first"); sys.exit(2)

    # Load ground truth from samples
    with SAMPLES_FILE.open() as f:
        gt = {s["sample_id"]: json.loads(line) if isinstance(line, str) else line
              for line, s in ((line, json.loads(line)) for line in f)}
    # Simpler:
    gt = {}
    with SAMPLES_FILE.open() as f:
        for line in f:
            s = json.loads(line)
            gt[s["sample_id"]] = s

    # Per-day baseline: median return_d5 across all sampled anchors that day.
    # Note: gen_day comes from anchor (original generation), not as_of, so
    # the baseline matches what outcomes.py computes in production.
    by_day: dict[str, list[float]] = {}
    for s in gt.values():
        day = s["generated_at"][:10]
        by_day.setdefault(day, []).append(s["return_d5"])
    day_baseline = {d: sorted(v)[len(v)//2] for d, v in by_day.items()}

    # Load every run
    runs: list[dict] = []
    for path in sorted(RUNS_DIR.glob("*.jsonl")):
        with path.open() as f:
            for line in f:
                try:
                    runs.append(json.loads(line))
                except Exception:
                    pass

    # Group by (model, mode)
    by_combo: dict[tuple, list[dict]] = {}
    for r in runs:
        key = (r["model"], r["mode"])
        by_combo.setdefault(key, []).append(r)

    rows = []
    for (model, mode), rs in sorted(by_combo.items()):
        rows.append(_score_combo(model, mode, rs, gt, day_baseline))

    _write_scoreboard(rows)
    _write_disagreements(by_combo, gt)
    logger.info("scoreboard → %s", SCOREBOARD_MD)
    logger.info("disagreements → %s", DISAGREEMENTS_MD)


def _score_combo(model: str, mode: str, rs: list[dict],
                 gt: dict[int, dict],
                 day_baseline: dict[str, float]) -> dict[str, Any]:
    """One scoreboard row. All the metrics we care about."""
    n_total = len(rs)
    n_ok = sum(1 for r in rs if r["schema_ok"])
    n_errors = n_total - n_ok

    # Per-verdict tallies (only schema-ok rows can score)
    by_verdict: dict[str, dict] = {}

    # Dedup: keep last (model, sample_id) record per (code, gen_day).
    # Since we replay each sample once per model, "last" = "only" — dedup
    # here just means "one per (code, gen_day) sampled anchor". Each
    # sample is already a unique (code, generated_at), but multiple
    # sample_ids may share (code, gen_day) if production re-anchored.
    last_per_key: dict[tuple, dict] = {}
    for r in rs:
        if not r["schema_ok"]:
            continue
        s = gt.get(r["sample_id"])
        if not s:
            continue
        key = (s["code"], s["generated_at"][:10])
        # Stable choice: the one with the latest generated_at
        prev = last_per_key.get(key)
        if prev is None or gt[prev["sample_id"]]["generated_at"] < s["generated_at"]:
            last_per_key[key] = r
    dedup_ids = {id(r) for r in last_per_key.values()}

    # window-format check — three formats prompt is supposed to use.
    # Patterns are lenient (substring match).
    def _window_ok(w: str | None) -> bool | None:
        if not w:
            return None
        return any(p in w for p in ("跌破", "交易日内", "本周内"))

    total_wall = 0.0
    total_in = 0
    total_out = 0
    wall_n = in_n = out_n = 0

    for r in rs:
        s = gt.get(r["sample_id"])
        if not s:
            continue
        # cost / latency for all rows (including failed ones — failure has cost too)
        if r.get("wall_time_s"):
            total_wall += r["wall_time_s"]; wall_n += 1
        if r.get("input_tokens"):
            total_in += r["input_tokens"]; in_n += 1
        if r.get("output_tokens"):
            total_out += r["output_tokens"]; out_n += 1

        if not r["schema_ok"]:
            continue
        actionable = r.get("actionable") or "?"
        b = by_verdict.setdefault(actionable, {
            "n": 0, "hits": 0, "n_unique": 0, "hits_unique": 0,
            "sum_return_d5": 0.0, "sum_excess_d5": 0.0,
            "valid_window_ok": 0, "valid_window_n": 0,
        })
        b["n"] += 1
        ret = s["return_d5"]
        day = s["generated_at"][:10]
        baseline = day_baseline.get(day, 0.0)
        b["sum_return_d5"] += ret
        b["sum_excess_d5"] += ret - baseline
        is_hit = ((actionable == "建议买入" and ret > 0) or
                  (actionable == "建议卖出" and ret < 0))
        if is_hit:
            b["hits"] += 1
        if id(r) in dedup_ids:
            b["n_unique"] += 1
            if is_hit:
                b["hits_unique"] += 1
        w_ok = _window_ok(r.get("valid_window"))
        if w_ok is not None:
            b["valid_window_n"] += 1
            if w_ok:
                b["valid_window_ok"] += 1

    return {
        "model": model,
        "mode": mode,
        "n_total": n_total,
        "n_ok": n_ok,
        "n_errors": n_errors,
        "schema_pass_pct": round(100 * n_ok / max(n_total, 1), 1),
        "verdicts": by_verdict,
        "wall_avg_s": round(total_wall / max(wall_n, 1), 2),
        "input_tokens_avg": int(total_in / max(in_n, 1)) if in_n else None,
        "output_tokens_avg": int(total_out / max(out_n, 1)) if out_n else None,
    }


def _write_scoreboard(rows: list[dict]) -> None:
    """Render scoreboard.md — one section per (model, mode), with the
    headline being excess_return_d5 on buy + sell, since per Rush's S0
    analysis those are the metrics that actually measure skill (not beta)."""
    lines: list[str] = []
    lines.append("# Replay eval scoreboard\n")
    lines.append(f"generated {datetime.now().isoformat()}  ·  {len(rows)} (model, mode) combos\n")
    lines.append("\n## Quick read\n")
    lines.append("\n*Excess return (d5)* is the metric that controls for market beta —")
    lines.append(" positive on 建议买入 = real selection alpha, negative on 建议卖出 = real timing skill.\n")

    # Compact summary table — one row per combo
    lines.append("\n## Headline metrics\n")
    lines.append("| Model | Mode | Schema OK | Buy excess d5 | Buy hit d5 (dedup) | Sell excess d5 | Sell hit d5 (dedup) | Wall avg | Out tok avg |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        buy = r["verdicts"].get("建议买入") or {}
        sell = r["verdicts"].get("建议卖出") or {}
        def _exc(b):
            return f"{b['sum_excess_d5']/max(b['n'],1):+.2f}pp · n={b['n']}" if b else "—"
        def _hit_dedup(b):
            if not b or not b.get("n_unique"):
                return "—"
            return f"{100*b['hits_unique']/b['n_unique']:.0f}% · n={b['n_unique']}"
        lines.append("| {model} | {mode} | {sok}% ({err} err) | {bex} | {bh} | {sex} | {sh} | {wall}s | {ot} |".format(
            model=r["model"], mode=r["mode"],
            sok=r["schema_pass_pct"], err=r["n_errors"],
            bex=_exc(buy), bh=_hit_dedup(buy),
            sex=_exc(sell), sh=_hit_dedup(sell),
            wall=r["wall_avg_s"], ot=r["output_tokens_avg"] or "—",
        ))

    # Per-combo detail
    lines.append("\n## Per-combo detail\n")
    for r in rows:
        lines.append(f"\n### {r['model']} · {r['mode']}\n")
        lines.append(f"- schema_pass_pct: **{r['schema_pass_pct']}%** ({r['n_ok']}/{r['n_total']}, {r['n_errors']} errors)")
        lines.append(f"- wall_avg_s: **{r['wall_avg_s']}s**")
        lines.append(f"- input_tokens_avg: {r['input_tokens_avg']}")
        lines.append(f"- output_tokens_avg: {r['output_tokens_avg']}")
        lines.append("")
        lines.append("| Verdict | n | n_unique | hit_rate | hit_rate_dedup | avg_return_d5 | excess_return_d5 | valid_window_ok |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for v, b in sorted(r["verdicts"].items()):
            directional = v in ("建议买入", "建议卖出")
            hit = f"{100*b['hits']/max(b['n'],1):.1f}%" if directional else "—"
            hit_d = (f"{100*b['hits_unique']/max(b['n_unique'],1):.1f}%"
                     if directional and b['n_unique'] else "—")
            avg_r = b['sum_return_d5'] / max(b['n'], 1)
            avg_e = b['sum_excess_d5'] / max(b['n'], 1)
            vw = (f"{100*b['valid_window_ok']/max(b['valid_window_n'],1):.0f}%"
                  if b['valid_window_n'] else "—")
            lines.append(
                f"| {v} | {b['n']} | {b['n_unique']} | {hit} | {hit_d} | {avg_r:+.2f}% | {avg_e:+.2f}pp | {vw} |"
            )

    SCOREBOARD_MD.write_text("\n".join(lines))


def _write_disagreements(by_combo: dict[tuple, list[dict]],
                          gt: dict[int, dict]) -> None:
    """Find samples where the actionable diverges across models. Output
    a sample-major view: per sample, the verdict from every (model, mode)
    that successfully scored, plus ground truth d5."""
    # Index runs by sample_id → list of (model, mode, actionable)
    by_sample: dict[int, list[tuple]] = {}
    for (model, mode), rs in by_combo.items():
        for r in rs:
            if r["schema_ok"] and r.get("actionable"):
                by_sample.setdefault(r["sample_id"], []).append(
                    (model, mode, r["actionable"], r.get("confidence")))

    interesting: list[tuple[int, list]] = []
    for sid, verdicts in by_sample.items():
        # Disagreement = >1 distinct actionable across the (model, mode) set
        distinct = set(a for _, _, a, _ in verdicts)
        if len(distinct) > 1:
            interesting.append((sid, verdicts))

    interesting.sort(key=lambda x: -len({v[2] for v in x[1]}))

    lines = [f"# Disagreement cases\n",
             f"\n{len(interesting)} samples with ≥2 distinct actionable verdicts across models.\n"]
    for sid, vs in interesting[:50]:
        s = gt[sid]
        lines.append(f"\n## #{sid}  {s['code']}  ({s['actionable']} in production, return_d5={s['return_d5']:+.2f}%)\n")
        lines.append("| Model | Mode | Verdict | Confidence |")
        lines.append("|---|---|---|---|")
        for model, mode, a, conf in vs:
            lines.append(f"| {model} | {mode} | {a} | {conf} |")
    DISAGREEMENTS_MD.write_text("\n".join(lines))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("samples", help="sample anchors → samples.jsonl")
    sp.add_argument("--n", type=int, default=300)
    sp.add_argument("--max-age-days", type=int, default=60)
    sp.add_argument("--seed", type=int, default=42)

    pr = sub.add_parser("prompts", help="render point-in-time prompts → prompts.jsonl")

    rn = sub.add_parser("run", help="call LLMs → runs/*.jsonl (resumable)")
    rn.add_argument("--model", action="append", default=None,
                    help="model name (repeatable). default: all")
    rn.add_argument("--mode", action="append", default=None,
                    choices=["single", "debate"],
                    help="mode (repeatable). default: single + debate")
    rn.add_argument("--workers", type=int, default=8)
    rn.add_argument("--max-samples", type=int, default=None,
                    help="only run the first N prompts (smoke test)")

    sub.add_parser("score", help="score runs → scoreboard.md + disagreements.md")

    al = sub.add_parser("all", help="samples → prompts → run → score")
    al.add_argument("--n", type=int, default=300)
    al.add_argument("--max-age-days", type=int, default=60)
    al.add_argument("--seed", type=int, default=42)
    al.add_argument("--workers", type=int, default=8)
    al.add_argument("--model", action="append", default=None)

    args = p.parse_args()
    if args.cmd == "samples":
        stage_samples(args.n, args.max_age_days, args.seed)
    elif args.cmd == "prompts":
        stage_prompts()
    elif args.cmd == "run":
        modes = args.mode or ["single", "debate"]
        stage_run(args.model, modes, args.workers, args.max_samples)
    elif args.cmd == "score":
        stage_score()
    elif args.cmd == "all":
        stage_samples(args.n, args.max_age_days, args.seed)
        stage_prompts()
        stage_run(args.model, ["single", "debate"], args.workers, None)
        stage_score()


if __name__ == "__main__":
    main()
