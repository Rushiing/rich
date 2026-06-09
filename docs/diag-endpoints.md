# Diagnostic & Migration Endpoints

All under `/api/_diag/*` on the backend. Three traits in common:

- **Public** — no auth required, intentional. They surface schema / counts /
  config, never user data, and `AUTH_DISABLED=true` is on in prod anyway.
  Each endpoint's docstring re-states this.
- **Idempotent** unless docstring says otherwise. Re-running is safe.
- **Long-running ones are async** — they fire a daemon thread and return
  `{started: true}` immediately, with a sibling `/status` endpoint for
  progress. Pattern was forced by Railway's HTTP proxy killing requests
  past ~30 s.

Prod base URL: `https://pure-emotion-production-6722.up.railway.app`

---

## Snapshot / schema

### `GET /api/_diag/snapshot-schema`
Lists the actual columns on the `snapshots` table from `information_schema`,
checks that the expected post-4/27 extras are present. First thing to call
when a field-related bug looks like a missing-column issue.

```bash
curl -s "$BASE/api/_diag/snapshot-schema" | python3 -m json.tool
```

Expected `{ok: true, missing_extras: []}`. If `missing_extras` is non-empty,
`ensure_extra_columns()` didn't run or failed — check startup logs.

---

## K-line cache

### `POST /api/_diag/refresh-klines`
One-shot manual run of the post-close `_kline_tick`. Synchronous,
~1 minute for a 60-code watchlist. Use when:
- First-time bootstrap before the 16:30 BJT cron has fired
- After adding new codes to the watchlist that need historical bars before
  the next post-close tick

### `GET /api/_diag/klines-status`
Per-code coverage stats on the `klines` table — total rows, distinct codes,
date window, min/median/max rows per code. **Health check** for anything
downstream that depends on K-line history (technical analysis, outcomes
backfill, 3-day rolling metrics).

```bash
curl -s "$BASE/api/_diag/klines-status" | python3 -m json.tool
```

If `rows_per_code_min` << `rows_per_code_median`, some codes are starved
(usually newly-added; kline_tick hasn't caught up yet).

---

## Financials

### `POST /api/_diag/refresh-financials`
Async one-shot bootstrap. Pulls 8 quarters per watchlist code via
akshare's sina endpoint. ~90 s for 60 codes — runs in a background
thread to dodge Railway's HTTP proxy timeout. Safe to re-run (upsert).

Returns `{started: true}` or `{started: false, already_running: true}`.

### `GET /api/_diag/refresh-financials/status`
Status of the most recent `/refresh-financials` run. Returns
`{running, progress: {done, ok, failed, total, current}, last_result}`.
`progress` is live; client can show "5/61 done, currently fetching
600519" instead of a black-box spinner.

---

## Industry

### `POST /api/_diag/refresh-industry-meta`
One-shot industry mapping pull. Phase 7 stores per-stock 行业 in the
`industry_meta` table; without this, snapshot jobs can't compute
industry-percentile chips. Call once after deploy or after adding new
stocks to the watchlist.

---

## Shareholder changes (6/9 — 内部人交易信号)

### `POST /api/_diag/refresh-shareholder`
**Async** market-wide pull of insider shareholding changes (董监高/高管/
配偶子女增减持) from 东方财富 datacenter directly. Filters to watchlist
codes + last 90 days, upserts into `shareholder_changes`.

Why this exists: 6/9 hit-rate analysis showed confidence 几乎全部落在 med
桶,high (≥80) 0 样本。LLM 缺"内部人信号"是其中一个原因。这个 endpoint
+ 17:30 cron 给 LLM 一个新硬信号:大股东减持 vs 高管增持 vs 中性活动。

```bash
curl -X POST "$BASE/api/_diag/refresh-shareholder"
# {"started": true}
# 等 30 秒
curl "$BASE/api/_diag/refresh-shareholder/status" | python3 -m json.tool
```

Implementation note: 直接 GET 东财 `RPT_EXECUTIVE_HOLD_DETAILS` 接口,自
control pagination (max 2 页 × 5000 行)。akshare 的 `stock_hold_management_detail_em`
会内部 paginate 所有页几十页,180s 超时。我们 control 后固定 20-30s。

### `GET /api/_diag/refresh-shareholder/status`
Status of the most recent run + in-flight progress (`rows_seen` /
`rows_upserted` / `failed`).

### `GET /api/_diag/akshare-shareholder-probe` (临时,等删)
Phase 0 probe endpoint 验证 akshare 接口名 + 字段。已用 detail_em
确认,留 1-2 天兜底然后删除。

```bash
# 看所有候选 fn 在 akshare 里是否存在
curl "$BASE/api/_diag/akshare-shareholder-probe" | python3 -m json.tool

# 跑单个 fn 的实际返回
curl "$BASE/api/_diag/akshare-shareholder-probe?fn=stock_hold_management_detail_em"
```

---

## Outcomes (analysis hit-rate feedback loop)

### `GET /api/_diag/outcomes-stats`
**Public hit-rate summary**, grouped by `(prompt_version, actionable)`.
A "hit" = 建议买入 with `return_d5 > 0`, or 建议卖出 with `return_d5 < 0`.
"观望" / "不建议入手" are not scored (no directional claim) — `hit_rate: null`.

```bash
curl -s "$BASE/api/_diag/outcomes-stats" | python3 -m json.tool
```

This is the *user-facing* report. If you only need a single dashboard
number, use this.

### `GET /api/_diag/hit-rate-by-confidence`
**Validation:** does the LLM's self-reported confidence actually correlate
with accuracy? Stratifies hit_rate by `(actionable, confidence_bucket)`
across d1/d3/d5 horizons in one shot. Bucket follows frontend's
`confidenceBucket()`:
- `high`: confidence >= 80
- `med`: 60-79
- `low`: < 60

Only buy/sell directional verdicts; excludes anchors from before 5/29
(confidence column added then, older rows have null). d5 is the gold
standard; d1/d3 light up earlier (5/29 anchors only reach d5 around
6/5) and give a preview.

Returns `scored_per_horizon` totals so you can see how much sample
you have to work with at each horizon. Each bucket has `d1`/`d3`/`d5`
sub-objects with `{n, hit_rate, avg_return}`.

**Expected pattern if confidence works**:
```
buy.high.hit_rate > buy.med.hit_rate > buy.low.hit_rate
sell.high.hit_rate > sell.med.hit_rate > sell.low.hit_rate
```

Flat distribution = LLM is throwing dice picking numbers; we'd need to
redesign confidence scoring. Wait 1-2 weeks of new anchors before drawing
conclusions — small bucket sizes are noisy.

```bash
curl -s "$BASE/api/_diag/hit-rate-by-confidence" | python3 -m json.tool
```

---

### `GET /api/_diag/outcomes-detail`
Raw distribution of the `analysis_outcomes` table — diagnoses why
`outcomes-stats` is sparse. Shows total / scored split by actionable,
distinct modes / prompt_versions, time window, fill counts per
horizon (`close_d1` / `d3` / `d5` / `d20`).

**Call when**: `outcomes-stats` reports `total_scored` lower than
expected, and you need to know whether the gap is on the write side
(anchors not being recorded) or the fill side (backfill cron not
keeping up).

### `GET /api/_diag/outcomes-kline-coverage`
Cross-table sanity check: do anchor `code` values have matching klines?
Returns:
- `orphan_codes` — codes with anchors but no klines (deleted from
  watchlist → `_kline_tick` stopped tracking them)
- `fill_stats` — per-horizon fill counts across all anchors
- `sample_unscored` — 5 oldest + 5 newest unfilled anchors with their
  `close_dN` values + `future_bars_after_gen_day` count

**Call when**: backfill reports `filled=0` but the data looks like it
should fill. This separates "anchor code disappeared from klines" from
"future bars don't exist yet" from "real algorithm bug".

### `POST /api/_diag/backfill-outcomes`
**Async** manual backfill. Walks `analysis_outcomes` rows where
`close_d20 IS NULL`, looks up future kline closes per row, fills
`close_dN` + `return_dN` for any horizon now satisfied. Daily cron at
17:00 BJT (`_outcomes_tick`) does this automatically — manual call is
for ad-hoc catch-up after a long outage or a kline_tick miss.

Returns `{started: true}` immediately. ~30-60 s for 1000+ anchors.

### `GET /api/_diag/backfill-outcomes/status`
Status of the most recent `/backfill-outcomes` run. Returns
`{running, last_result: {scanned, filled}}`.

`scanned` = anchors examined; `filled` = anchors where at least one
new horizon got a value. `filled` can legitimately be 0 if every
in-flight anchor has already filled to the farthest horizon possible
given current kline data.

---

## Admin batch (one-shot)

### `POST /api/_diag/regenerate-all`
**Async** force re-analyze every distinct watchlist code, bypassing the
stale/missing skip ladder AND the snapshot_id cache. Use after shipping
a new schema field (e.g. `valid_window` on 6/3) when you want all rows
to carry it before the next market open.

`distinct_codes ~ 100` from `/watchlist-stats` → ~10 min @ 5-7s/call,
cost ~5 元. Returns `{started: true}` immediately or
`{started: false, already_running: true}`.

```bash
curl -X POST "$BASE/api/_diag/regenerate-all"
# Poll status:
curl -s "$BASE/api/_diag/regenerate-all/status" | python3 -m json.tool
```

### `GET /api/_diag/regenerate-all/status`
Status of the most recent run: `{running, last_started_at, last_result}`
where `last_result` is `{codes, generated, failed, skipped}` once done.

---

## Migrations (one-off)

### `GET /api/_diag/smart-analyze-status`
Status of the smart intraday analysis tick (6/3, every 30 min in trading
hours). Returns the **last run's** per-reason counters:

```json
{
  "running": false,
  "last_started_at": "2026-06-03T05:35:00+00:00",
  "last_result": {
    "distinct_codes": 100,
    "triggered": 12,
    "generated": 11,
    "cache_hit": 1,
    "failed": 0,
    "by_reason": {
      "cooldown": 8,
      "no_change": 65,
      "price_move": 10,
      "signal_change": 2,
      "stale": 0
    },
    "triggered_codes": ["600519", "300750", ...]
  }
}
```

**Useful for tuning thresholds**: if `stale` dominates we should lower
`_SMART_PRICE_DELTA_PCT`; if `cooldown` dominates we're scanning too
often. `by_reason` always sums to `distinct_codes`.

---

### `POST /api/_diag/migrate-prompt-version`
**Idempotent** retroactive fix for the pre-c231b60 hardcode bug:
`PROMPT_VERSION` was a module constant `"v2.5-debate"`, so every row
was tagged the same regardless of mode. This endpoint splits the bucket
using the `mode` column:
- `prompt_version='v2.5-debate'` AND `mode='debate'` → unchanged
- `prompt_version='v2.5-debate'` AND `mode!='debate'` → `'v2.5-single'`

Touches both `analyses` and `analysis_outcomes`. Returns row counts.
Already run on prod 2026-05-28; re-running is a no-op.

### `POST /api/_diag/migrate-confidence-to-int`
**Idempotent** migration tied to the confidence-as-integer rollout.
Pre-5/28 `key_table.confidence` was an enum `"高"/"中"/"低"`; from this
deploy on it's a 0-100 integer. This endpoint rewrites the JSON in
place using `jsonb_set`:
- `"高"` → `85`
- `"中"` → `65`
- `"低"` → `45`
- integer or null → untouched

WHERE clause only catches the enum strings, so re-running is safe.
Postgres only (skipped on SQLite smoke tests). Returns `{rows_updated: N}`.

Call once after the deploy that ships the integer schema. Frontend reads
`confidence: string | number` and uses `confidenceBucket()` so old rows
still render correctly even before this is run — but running it lets
the detail page show the actual number instead of just a bucket label.

---

## Pattern reference

When adding a new diag endpoint, mirror the existing shape:

- **GET** for read-only inspection (returns JSON dict, no side effects)
- **POST** for actions (`refresh-*`, `backfill-*`, `migrate-*`)
- If the action can exceed ~30 s, **always async**: thread + module-level
  `{v: bool, last_result: ...}` + sibling `/status` endpoint. Synchronous
  long calls get killed by Railway's proxy.
- Add a module-level `_<name>_lock = threading.Lock()` if multiple
  concurrent runs would corrupt state.
- Public on purpose — `/_diag/*` is the convention. Don't put PII or
  secrets in responses.
- Document in this file with: path, method, what it does, when to call.
