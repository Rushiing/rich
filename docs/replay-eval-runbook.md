# Replay LLM Evaluation — runbook

How to run the head-to-head evaluation comparing candidate models (火山 ARK
coding plan) against the current `kimi-k2.5` baseline.

Why this exists: see the docstring of `backend/replay_eval.py`. TL;DR:
replay on already-known forward returns gives A/B-grade rigor in hours
instead of weeks of live data collection.

---

## Models under test

Baseline:
- `kimi-k2.5` (current production, via dashscope)

Candidates (火山 ARK coding plan, Anthropic-compatible endpoint):
- `kimi-k2.6` — direct successor of baseline
- `deepseek-v4-pro` — DeepSeek 旗舰非 code
- `glm-5.2` — 智谱旗舰, 1M context
- `doubao-seed-2.0-pro` — 豆包旗舰
- `minimax-m3` — minimax 新旗舰

Edit `build_default_models()` in `replay_eval.py` to swap any candidate.

## Pre-flight (already verified)

Smoke test on 4 samples + `kimi-k2.6` × single confirmed:
- DB connection from local machine → Railway Postgres works
- Stratified sampling picks the right verdict mix from `analysis_outcomes`
- Point-in-time prompt renderer wraps `_user_prompt` with kline/financials
  cutoffs (monkey-patch within a context manager, serial only)
- ARK Anthropic-compat endpoint accepts the existing `submit_analysis`
  tool schema (forced `tool_choice` works on `kimi-k2.6`)
- Score stage produces `scoreboard.md` + `disagreements.md`

One sample wall time on `kimi-k2.6`: ~42s single, ~126s debate (3 calls).

## Eligible anchor inventory (as of 6/19)

3321 anchors in the last 30 days with `return_d5` filled. Distribution:
- 观望: 2181
- 建议卖出: 550
- 不建议入手: 455
- 建议买入: 135  ← the binding constraint

Targets per bucket (in `TARGET_PER_BUCKET`):
买80 / 卖80 / 观80 / 不60 = 300. All comfortably within supply.

## Environment

Set in your shell (don't commit):

```bash
export DATABASE_URL='postgresql+psycopg://postgres:<PWD>@shuttle.proxy.rlwy.net:12863/railway'

# Baseline route — same as production analysis.py uses
export ANTHROPIC_API_KEY='<dashscope kimi key>'
export ANTHROPIC_BASE_URL='<dashscope base url>'

# Candidate route — 火山 ARK coding plan, Anthropic-compatible
export VOLCENGINE_BASE_URL='https://ark.cn-beijing.volces.com/api/coding'
export VOLCENGINE_API_KEY='ark-cd263b71-c699-4f15-8fea-44b701d832a8-a4059'
```

`DATABASE_URL` needs the password from Railway → Postgres service →
Variables. The DB connection is read-only in spirit (we only `SELECT`)
but uses the regular `postgres` superuser.

## The actual run

```bash
cd backend

# Stage 1 + 2: sample 300 anchors + render point-in-time prompts
# (Fast — minutes, not LLM calls.)
.venv/bin/python replay_eval.py samples --n 300 --max-age-days 60 --seed 42
.venv/bin/python replay_eval.py prompts

# Stage 3: 6 models × 2 modes × 300 samples = 3600 single + 5400 debate calls
# (debate is 3 LLM calls per sample, so 7200 LLM round-trips total)
.venv/bin/python replay_eval.py run --workers 16

# Stage 4: score everything in runs/ → scoreboard.md + disagreements.md
.venv/bin/python replay_eval.py score
```

Or one-shot:

```bash
.venv/bin/python replay_eval.py all --n 300 --workers 16
```

## Wall time + cost estimate

Per-call (observed on kimi-k2.6, expect candidates within 2×):
- single: ~42s, ~5000 input + ~3000 output tokens
- debate: ~126s (3 sequential calls), ~15000 input + ~9000 output tokens

12 dispatch rounds (6 models × 2 modes), each runs all 300 samples with
the configured worker count. At `--workers 16`:
- single round: 300 × 42s / 16 ≈ 13 min
- debate round: 300 × 126s / 16 ≈ 40 min
- Total: 12 × ~26 min ≈ **5.3 hours**

At `--workers 8` → ~10.5 hours (good for overnight).

Tokens total: ~7200 calls × ~8K avg = ~57M total tokens. Both 火山 coding
plan and dashscope kimi plan are flat monthly fee; usage should be inside
caps. Watch for 429 / quota errors in the run logs.

## Resumability

Each LLM call's result is JSONL-appended to `runs/<model>_<mode>.jsonl`
immediately. If the run crashes / loses network / OOM, just re-run the
same command — `_already_done()` reads back the file and skips done
sample_ids. Workers themselves are stateless.

To force a clean rerun for one combo: delete its `runs/<model>_<mode>.jsonl`.

## What "winning" looks like (Phase 1)

Read `scoreboard.md` headline table. Per-model headline metrics, in
priority order:

1. **`buy excess d5`** (most important) — positive % means real selection
   alpha beyond market beta. Baseline kimi-k2.5 had +8.5pp at last check.
2. **`sell excess d5`** — negative % is good (predicted "down" things go
   down more than the day's baseline). Baseline was -0.2pp ≈ noise.
3. **`schema OK %`** — should be ≥98%. Lower means the candidate doesn't
   reliably emit the structured tool call; engineering blocker.
4. **`hit_rate_dedup`** — secondary; correlated with excess but doesn't
   control for beta.
5. **`out_tokens_avg`** — proxy for cost (火山 plan is fixed but token
   spend hints at chattiness; less = better).
6. **`wall_avg`** — user-facing latency.

A candidate wins if it beats baseline on (1) AND keeps (3) at the same
level, with reasonable showings on the rest.

## Phase 2 (after Phase 1 shortlist)

Open `disagreements.md`. It lists samples where the actionable verdict
diverged across the (model, mode) set, sorted by how many distinct
verdicts each sample drew. The first 50 are dumped — read them side by
side with the `runs/*.jsonl` raw payloads to judge:

- Whose `deep_analysis` is more on-voice for our 克制研究员 register
- Whose red_flags surface real risks vs sycophantic ones
- Whether debate-mode actually adds value over single for this candidate

This is judgment work; the script doesn't automate it.

## Phase 3 (if a winner emerges)

Plug the winner into the existing live A/B mechanism:

```bash
# Railway → backend → Variables
ANALYSIS_MODEL_B=<winner>
ANALYSIS_AB_PCT=30
```

Caveat in `config.py`: model B currently routes through the same
ANTHROPIC_BASE_URL as model A — for an ARK-only winner, you'd need to
either change ANTHROPIC_BASE_URL → ARK (and migrate baseline along) or
extend the A/B mechanism to support per-model base_urls. The latter is
~30 lines in `analysis.py`.

## Output layout

```
backend/eval_out/
├── samples.jsonl              # the 300 stratified anchors
├── prompts.jsonl              # point-in-time user_prompt per sample
├── runs/
│   ├── kimi-k2.5_single.jsonl
│   ├── kimi-k2.5_debate.jsonl
│   ├── kimi-k2.6_single.jsonl
│   ├── kimi-k2.6_debate.jsonl
│   ├── deepseek-v4-pro_single.jsonl
│   └── ...
├── scoreboard.md              # headline summary + per-combo detail
└── disagreements.md           # cases where models disagreed on actionable
```
