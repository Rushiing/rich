"""Replay-eval orchestration endpoints — Plan C.

Why these exist: the eval involves a 4-hour LLM run that needs stable DB
access for the sample/prompt prep phases. Running it from a developer
machine over Railway's Postgres proxy proved unstable; running it from
inside the backend service (same Railway datacenter as the DB) is the
reliable path.

Start kicks off `backend/replay_eval.py all` as a subprocess so we don't
need to massage the script's module-level state to be FastAPI-friendly.
Output lands in the script's own `eval_out/` directory; the status +
result endpoints just inspect those files.

Endpoints are deliberately unauthenticated (matches other /_diag/* in
this app) so curl from anywhere works.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/_diag/replay-eval", tags=["diag"])

# Anchor everything off the script location so paths work the same in dev
# and in the Railway container.
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "replay_eval.py"
_OUT_DIR = _SCRIPT_PATH.parent / "eval_out"
_LOG_FILE = _OUT_DIR / "run.log"
_PID_FILE = _OUT_DIR / "run.pid"

# Single in-process lock so concurrent /start requests can't double-spawn.
# The subprocess itself is also process-bounded via the PID file.
_lock = threading.Lock()


def _is_running() -> tuple[bool, int | None]:
    """Return (alive, pid) by checking the PID file + actually probing the
    process. PID file alone lies after crashes / restarts."""
    if not _PID_FILE.exists():
        return False, None
    try:
        pid = int(_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return False, None
    try:
        # signal 0 = "are you there?" without delivering
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False, pid
    return True, pid


@router.post("/start")
def start_eval(n: int = 300, workers: int = 10, max_age_days: int = 60) -> dict[str, Any]:
    """Spawn `replay_eval.py all` as a child process.

    Idempotent against double-clicks: a second /start while one is running
    returns {started: False, already_running: True, pid: ...}.

    Required env on the service:
      - DATABASE_URL (already set for the app)
      - ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL (baseline kimi)
      - VOLCENGINE_API_KEY + VOLCENGINE_BASE_URL (candidates on ARK)
    Missing VOLCENGINE_* makes the script skip those models (logged + the
    runs file just won't have lines).
    """
    with _lock:
        alive, prev_pid = _is_running()
        if alive:
            return {"started": False, "already_running": True, "pid": prev_pid}

        if not _SCRIPT_PATH.exists():
            raise HTTPException(500, f"replay_eval.py not found at {_SCRIPT_PATH}")

        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        # Truncate the log on a fresh start so /status only shows new output.
        # JSONL run files are NOT truncated — those carry resume state for
        # individual (model, mode) combos.
        _LOG_FILE.write_text(f"# launched at {datetime.now(timezone.utc).isoformat()}\n")

        # Run from the script's parent dir so any relative paths inside the
        # script resolve consistently. Detached from the FastAPI process so
        # `uvicorn` reloads / killer signals don't take it down with them.
        proc = subprocess.Popen(
            [sys.executable, str(_SCRIPT_PATH), "all",
             "--n", str(n), "--workers", str(workers),
             "--max-age-days", str(max_age_days)],
            cwd=str(_SCRIPT_PATH.parent),
            stdout=open(_LOG_FILE, "ab", buffering=0),
            stderr=subprocess.STDOUT,
            start_new_session=True,  # decouple from FastAPI's process group
            env=os.environ.copy(),
        )
        _PID_FILE.write_text(str(proc.pid))
        return {"started": True, "pid": proc.pid, "out_dir": str(_OUT_DIR)}


@router.get("/status")
def status_eval(log_tail: int = 30) -> dict[str, Any]:
    """Snapshot of the eval's progress. Designed to be polled.

    Returns:
      - alive: is the subprocess still running
      - pid
      - log_tail: last N lines of run.log so you see what stage / errors
      - samples_done, prompts_done: line counts of the prep files
      - runs: per-(model, mode) lines done, oldest_modified_at
      - artifacts: which result files exist
    """
    alive, pid = _is_running()
    log_lines: list[str] = []
    if _LOG_FILE.exists():
        # Tail without loading the whole file (it can grow to MB)
        try:
            with _LOG_FILE.open("rb") as f:
                f.seek(0, 2)
                end = f.tell()
                read_size = min(end, 64 * 1024)
                f.seek(end - read_size)
                tail_bytes = f.read()
            text = tail_bytes.decode("utf-8", errors="replace")
            log_lines = text.splitlines()[-log_tail:]
        except OSError:
            pass

    samples_done = _line_count(_OUT_DIR / "samples.jsonl")
    prompts_done = _line_count(_OUT_DIR / "prompts.jsonl")

    runs_dir = _OUT_DIR / "runs"
    runs: list[dict[str, Any]] = []
    if runs_dir.exists():
        for path in sorted(runs_dir.glob("*.jsonl")):
            try:
                stat = path.stat()
                runs.append({
                    "combo": path.stem,
                    "lines": _line_count(path),
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc).isoformat(),
                    "size_bytes": stat.st_size,
                })
            except OSError:
                continue

    artifacts = {
        "scoreboard": (_OUT_DIR / "scoreboard.md").exists(),
        "disagreements": (_OUT_DIR / "disagreements.md").exists(),
    }

    return {
        "alive": alive,
        "pid": pid,
        "log_tail": log_lines,
        "samples_done": samples_done,
        "prompts_done": prompts_done,
        "runs": runs,
        "artifacts": artifacts,
    }


@router.get("/results/{name}")
def results_eval(name: str) -> dict[str, Any]:
    """Return the content of a result markdown file. `name` ∈ {scoreboard,
    disagreements}. Plaintext returned inside a JSON envelope so curl
    output stays one-shot."""
    allowed = {"scoreboard": "scoreboard.md", "disagreements": "disagreements.md"}
    if name not in allowed:
        raise HTTPException(404, f"unknown result; expected one of {sorted(allowed)}")
    path = _OUT_DIR / allowed[name]
    if not path.exists():
        raise HTTPException(404, f"{path.name} not yet generated")
    return {"path": str(path), "content": path.read_text()}


@router.post("/stop")
def stop_eval() -> dict[str, Any]:
    """Kill the running subprocess. Useful if you launched with wrong
    args or want to bail out partway. Won't touch result files."""
    alive, pid = _is_running()
    if not alive or pid is None:
        return {"alive": False, "killed": False}
    try:
        # Negative pid kills the whole process group (the subprocess +
        # any LLM-call threads). We spawned with start_new_session so the
        # process group exists and is distinct from FastAPI's.
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    return {"alive": True, "killed": True, "pid": pid}


@router.get("/errors/{combo}")
def errors_eval(combo: str, limit: int = 50) -> dict[str, Any]:
    """For a given (model_mode) combo, return aggregated error strings +
    a few sample lines. Used to diagnose 'why did this model fail so much'
    — was it rate limit (429), context overflow, schema drift, or what.

    `combo` is the file stem, e.g. `kimi-k2.5_single` or `glm-5.2_debate`.
    """
    runs_dir = _OUT_DIR / "runs"
    path = runs_dir / f"{combo}.jsonl"
    if not path.exists():
        raise HTTPException(404, f"{path.name} not found")

    import json as _json
    from collections import Counter
    error_counter: Counter = Counter()
    samples: list[dict[str, Any]] = []
    total = ok = 0
    with path.open() as f:
        for line in f:
            try:
                r = _json.loads(line)
            except Exception:
                continue
            total += 1
            if r.get("schema_ok"):
                ok += 1
                continue
            err = (r.get("error") or "no error string")[:200]
            error_counter[err] += 1
            if len(samples) < min(limit, 10):
                samples.append({
                    "sample_id": r.get("sample_id"),
                    "code": r.get("code"),
                    "wall_time_s": r.get("wall_time_s"),
                    "error": err,
                })
    return {
        "combo": combo,
        "total": total,
        "schema_ok": ok,
        "errors_total": total - ok,
        "error_groups": [
            {"count": c, "error": e} for e, c in error_counter.most_common(limit)
        ],
        "samples": samples,
    }


@router.get("/debug")
def debug_eval() -> dict[str, Any]:
    """Filesystem peek — what's actually in /app/eval_out, the log
    content if any, env presence for the keys the script needs. Used
    to diagnose 'I called /start, /status shows nothing' situations."""
    info: dict[str, Any] = {
        "script_path": str(_SCRIPT_PATH),
        "script_exists": _SCRIPT_PATH.exists(),
        "out_dir": str(_OUT_DIR),
        "out_dir_exists": _OUT_DIR.exists(),
        "cwd_from_start": str(_SCRIPT_PATH.parent),
        "env_present": {
            "DATABASE_URL": bool(os.environ.get("DATABASE_URL")),
            "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "ANTHROPIC_BASE_URL": bool(os.environ.get("ANTHROPIC_BASE_URL")),
            "VOLCENGINE_API_KEY": bool(os.environ.get("VOLCENGINE_API_KEY")),
            "VOLCENGINE_BASE_URL": bool(os.environ.get("VOLCENGINE_BASE_URL")),
        },
    }
    if _OUT_DIR.exists():
        info["out_dir_contents"] = sorted(
            f"{p.name} ({p.stat().st_size}B)" for p in _OUT_DIR.iterdir()
        )
        if _LOG_FILE.exists():
            info["log_full"] = _LOG_FILE.read_text(errors="replace")
        if _PID_FILE.exists():
            info["pid_file_content"] = _PID_FILE.read_text().strip()
    return info


@router.delete("/reset")
def reset_eval(confirm: str = "") -> dict[str, Any]:
    """Wipe eval_out/ entirely. Pass ?confirm=yes to actually do it.
    Forbidden while a run is in flight."""
    if confirm != "yes":
        raise HTTPException(400, "pass ?confirm=yes to wipe eval_out/")
    alive, pid = _is_running()
    if alive:
        raise HTTPException(409, f"eval still running (pid={pid}); /stop first")
    import shutil
    if _OUT_DIR.exists():
        shutil.rmtree(_OUT_DIR)
    return {"wiped": True}


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        with path.open("rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0
