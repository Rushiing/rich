# CLAUDE.md

> Handoff context for Claude. **Read this first** at the start of every session.
> When you make a meaningful new decision or finish a phase, update this file.

## What this product is

**rich** — an A股盯盘与深度解析工具，仅 for 不超过 10 人的内部使用。

The core loop:
1. User maintains a watchlist of A-share codes (paste/import).
2. Backend scrapes hourly snapshots during trading hours.
3. UI shows a "盯盘" view of all watched stocks with key metrics + signals.
4. User can drill into any stock for an LLM-generated deep analysis: a structured key table + a ~500-word markdown analysis.

This is an MVP for a small trusted group. **Do not over-engineer.** No multi-tenancy, no per-user auth, no audit log, no rate limiting. One shared password.

## Spec decisions (locked unless user changes them)

| # | Decision | Notes |
|---|---|---|
| Market | A股 only | Data via `akshare` (free, no token) |
| Hourly info | 行情 + 资金流 + 北向 + 新闻 + 公告 + 龙虎榜（收盘） | Schedule per A-share trading hours, not 24h |
| Deep analysis | First-click realtime LLM call, cache 4h, manual refresh button | Tool-use for the structured key table |
| Strategy framework | LLM judges freely in MVP, but leave a slot in the prompt-assembly layer | Future: structured rules plug in here |
| User scale | <10 people, single shared password | No auth UI, no multi-tenant |
| Excel import | Only recognize the "code" column | 6-digit, auto-detect 沪/深 |
| Strong signals | Highlighted red in UI | No push notifications |
| Position recommendation | % suggestion only | Don't ask user for total funds (sensitive) |
| Hourly snapshots | Stored only, no UI for history | Future: timeline view |
| Layer 2 length | ~500 words, one-page read | Future: layer 3 for deep dive |

## Phase plan

- [x] **Phase 0** — Skeleton: Next.js + FastAPI + Postgres + single-password login
- [x] **Phase 1** — Watchlist CRUD + paste/Excel import + akshare code validation
- [x] **Phase 2** — APScheduler + akshare scrapers + signals engine + 盯盘 view
- [x] **Phase 3** — Strategy-slot prompt + Claude tool-use key table + 500-word markdown + 4h cache
- [x] **Phase 4** — Mobile responsive pass + PWA polish (manifest + icon + install meta)
- [x] **Phase 5** — Folded into Phase 9.
- [x] **Phase 6** — Account system + SMS login (5/9–5/10). User table + per-user watchlist; cookie v2 with `uid`; Aliyun SMS stubbed (prod uses dev whitelist + 8888 until Aliyun template approved); ADMIN_PHONE migration backfills legacy rows.
- [x] **Phase 7** — Field reshuffle (`change_pct_3d` / `turnover_rate_3d` / `net_flow_3d`) + industry context (`industry_name` + 3 percentile chips + `industry_pe_avg/pb_avg`). 3-day data via THS `stock_fund_flow_individual('3日排行')` (~30min cache); industry mapping via CNINFO `stock_profile_cninfo` (eastmoney's individual-info endpoint blocked on Railway).
- [x] **Phase 8** — `/sectors` page (Sina sector spot, 49 industries sorted by today's change) + `actionable_tiers` (激进/中立/保守 同 prompt 一次输出，frontend toggle 即时切换).
- [x] **Phase 9** — K-line + technical indicators + 次日预判. Source switched to Tencent qt.gtimg.cn after akshare's `stock_zh_a_hist` (push2his.eastmoney) hit Railway block. Hand-rolled MA/MACD/BOLL/KDJ/RSI (pandas-ta unavailable on the wheel). New strong signals: `breakout_20d`, `below_ma60`. Soft signals: `macd_golden_cross`, `macd_death_cross`. `next_day_outlook` (trend / target_low/high / reasoning / confidence) lands in `key_table`.
- [x] **B0/B1/B2(v1) — 虚拟预选池** (6/10). `PoolEntry` (virtual_pool table, BigInteger PK with sqlite Integer variant) + `services/virtual_pool.py`. States observing→recommendable→(recommended reserved for B3)→eliminated; eliminated rows keep final metrics. Price basis is the kline table ONLY (pool candidates from sector_picks are usually outside every watchlist — no snapshots exist for them; `kline.pull_one` fetches per-code at entry/eval). Entry channels tagged via `source`: rules (watchlist universe, breakout_20d+big_inflow signals + profit_yoy>0 + non-ST) and sector_picks (today's cached picks only — never forces an LLM call). Evaluation in the same daily tick (cron 16:45 BJT, after klines 16:30): eliminate on close < entry×0.93 or ≥3 days below MA20; promote observing→recommendable on ≥5 trading days + positive return + ≥MA20. **Deliberate v1 narrowing**: thesis = machine-verifiable price rules + entry evidence, NOT free-text LLM catalysts (the valid_window unverifiability lesson); thesis.invalidation_rule text mirrors exactly what the code checks. Read API `GET /api/pool`, page `/pool` (TopNav), diag POST `/api/_diag/pool-tick` (async) + GET `/api/_diag/pool-status`.
- [x] **S2 — 卖出表达 + 详情页结论先行** (6/10). Detail page reordered so reading order = decision order: FreshnessBar + KeyTableCard (the verdict card) first, HoldingCard next, IndustryContext/AnalysisHistory demoted below, deep_analysis collapsed by default behind a "展开完整分析 (~N 字 · M 节)" button (auto-expands + scrolls on debate regen / banner click via `jumpToDebateSection`). Verdict card gained a trigger-price line (sell → 卖出区间 + 跌破即离场 with distance-to-current-%; buy → 买入区间 + 止损; hold → 若持有跌破离场; stop = highest stop_loss_level). Hit-rate UI switched to honest stats everywhere: big number = `hit_rate_dedup`, plus same-day-baseline `excess_return_d5` colored green only when it SUPPORTS the verdict (buy>+1 / sell<-1), amber otherwise ("主要是行情") — `/api/stocks/hit-rate-summary` now carries n_unique/hit_rate_dedup/excess_return_d5. List-page三层 not rebuilt: S1 action banner + existing verdict-group folding already covers it.
- [x] **S1 — 持仓感知卖出触发** (6/10). `services/action_items.py` + `GET /api/stocks/action-items` (declared before `/{code}` — literal path must win routing): per-holding checks for stop-loss breach (deepest level) / sell verdict / lapsed valid_window (machine-checks only the prompt-mandated formats: 跌破 X.XX / N 个交易日内 / 本周内; event windows skipped, no false alarms) / new strong signal vs the analysis anchor snapshot (bearish → urgent). Computed per request, no cron — spec excludes push, the 盯盘 banner is the surrogate. Frontend: 「今日需行动」banner atop /stocks (renders only when items exist, refreshes with the row list); detail-page ScenarioAdviceCard highlights the user's real quadrant from holding cost basis (±10% = 大幅), holding state lifted out of HoldingCard via onHoldingChange callback.
- [x] **S0 — 度量地基** (6/10). New goals locked — 卖出做到「准 + 简洁有说服力」, 买入走「虚拟预选池先观察后推荐」; full path in ROADMAP.md (S0-S3 卖出线 / B0-B3 买入线). S0 shipped: (a) **report_date string-parse bug** — `Financial.report_date` is "YYYYMMDD" text, the freshness check raised on every call since financials landed → every analysis silently lost the 25-pt financial dimension, depressing completeness + confidence across the board; also threshold 60→135d to match A-share disclosure calendar. (b) Outcome anchors now carry `model` / `nd_trend` / `nd_confidence` / `anchor_close` (qfq close of anchor day — dividend-safe basis vs the unadjusted intraday anchor_price). (c) Honest stats: `excess_return_d5` (vs same-generation-day all-anchor median — strips market beta), `n_unique`+`hit_rate_dedup` (last anchor per code-day — strips smart-cron clustering inflation), and `nd_outlook_stats()` finally scores next_day_outlook against real d1 (diag `/api/_diag/nd-outlook-stats`). (d) 主营业务 from CNINFO stored in `industry_meta.business_desc`, injected into the prompt 标的 section — closes the company-knowledge hallucination surface; 行业均值/分位 lines suppressed when industry peer pool < 3 (they were self-referential). (e) Number-grounding audit (`audit_number_grounding` in analysis_validators) — bolded numbers in deep_analysis checked against prompt input, **log-only** until false-positive rate is known. (f) Model A/B: `ANALYSIS_MODEL_B` + `ANALYSIS_AB_PCT` env, deterministic sha1(code + BJT day) % 100 bucketing, same-gateway constraint, default off — turns the 4/28 n=1 model choice into a data question.

- [x] **Deep 档 — 单股深挖** (6/29). 详情页「🧠 深度研究」按钮:`mode="deep"` 走一条独立的 OpenAI 兼容协议路径(`services/analysis_thinking.py`,`httpx.stream` + `tool_choice="auto"`),换用 **qwen3.7-max**(thinking),~90s。**为什么不复用 Anthropic 路径**:该 provider(token-plan MaaS)的 Anthropic 兼容层对 thinking 模型坏 —— force tool_choice 要么 400(deepseek/qwen 强制 thinking),要么静默吐损坏 tool JSON(kimi 系列);只有 OpenAI 协议 + stream + auto 能跑通结构化输出。deep 产出与 Anthropic 路径**同形状的 payload dict**,复用 `generate()` 后半全部 validate/持久化/`record_anchor` 逻辑(零改动);`prompt_version_for("deep")`→`v2.5-deep`,outcome anchor 独立 bucket,深挖战绩独立攒(不借其它档信用)。走 e0150bd 的异步后台路径(`_run_single_analysis_in_background` 透传 mode)→ deep 90s 不受 Railway 30s 代理限制。**默认关闭**:`ANALYSIS_DEEP_MODEL` 空 = 禁用(mode=deep 返回 503)。env:`OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY`(空则 fallback `ANTHROPIC_API_KEY`)/ `ANALYSIS_DEEP_MODEL` / `ANALYSIS_DEEP_ENABLE_THINKING`(qwen/deepseek stream 下默认就 thinking 且更快,默认 false;只有 kimi 系列需 true)。**质量结论(n=1 hard-case)**:thinking 比非 thinking 只边际提升(+0~1 红旗、分析更长),核心判断(action/confidence/tag)不变,速度代价 2.4-6.9×(36s→88-248s)→ 不适合 batch,只作单股深挖。UI 不透出 model name / "thinking" 等技术语言。

- [x] **持仓立场轴** (7/2). 根因:`actionable` 枚举混两个受众(建议买入/观望/不建议入手 → 未持仓者;建议卖出 → 持仓者),603986 出现大字"不建议入手"×情景卡"浮盈分批卖出"的立场打架 —— 不是模型矛盾,是展示层选错受众。Rush 拍板:**盯盘池默认视为已持仓、不揣测盈亏** → `holding_small`(小幅波动)正是盈亏无关的默认象限。落地:(a) 漏斗默认改 持有·平·激进(原 盈);(b) 详情页大字在持有态由所选象限 `scenario_direction` 驱动(看空→建议减仓/离场,`holderStanceFor` in holdingFunnel.ts,详情/列表共用),actionable 降为"未持仓视角"小字注;战绩口径跟切 hit-rate-summary 新增的 `by_scenario`(来自 scenario_hit_stats);触发价行 sellish 判定同轴。(c) 列表页 chip/分组/筛选统一走 `rowVerdict` 派生(显式标未持 → actionable;其余默认持仓立场),AnalysisBrief 新增 `holder_direction/holder_advice`;HeldToggle 未标过的行默认弱化"持有"。顺带加回现价列(StockRow.price)。(d) S1 检查范围从 Holding 表扩成**默认持仓全集**(自选 ∪ Holding − 漏斗显式未持),新增 `sell_stance`(warn):用户象限方向看空且 actionable≠建议卖出 —— 补上 603986 类"不建议入手但持仓者该减仓"的静音漏洞;象限判定:成本价浮盈亏(±10%)> 漏斗盈亏档 > holding_small。(e) prompt 新增 actionable 受众语义段(持仓者动作必须写进 scenario_advice、方向不许与顶部打架)→ **PROMPT_VERSION_BASE bump v2.6**;hit-rate 公示改为合并所有 `-single` 版本(n 加权,per-version 看 diag)。validator 新增 `_validate_stance_consistency`(拦 买入×持仓看空 等真矛盾,只记 correction)+ `_validate_price_fields`(7/2 生产事故:MiniMax-M2.5 无视 required 吐 sell_price null → 详情页 toFixed 崩;前端全面 null 防护 + 后端成对置空+可见 correction)。

- [x] **持仓立场轴收尾** (7/2 晚). (a) **盘后 smart tick 15:10/16:10**(`smart_analyze_postclose` in cron.py):盘中 smart cron 最后一跳 14:35,涨跌停等信号 15:00 收盘 snapshot 才落、龙虎榜/公告 16:00 盘后 pass 才有 → 「新强信号,当前建议未纳入」会挂到次日 09:35(沪电股份 limit_down 案例);复用 `run_smart_intraday_analysis`,只重解析信号真变了的票。(b) 首页 dashboard 换持仓立场轴(`verdictFor`/`worthAttention` 吃 `holder_direction`)—— 原来"不建议入手+持仓看空"的票不进「今日要看」。(c) HeldToggle 点选乐观同步 funnelMap,chip/分组/筛选即时切轴(原来要刷新页面)。(d) `scenario_hit_stats` 加 `-single` 版本过滤,by_scenario 与 by_actionable 同口径。**已知接受项**:7/2 前的 FunnelChoice 里 pnl=盈 有旧默认值污染(用户只点过风险档时上报的 盈 非本意),存量无法区分、增量已干净;action-items 每请求全量扫 FunnelChoice + per-code N+1 snapshot 查询,≤10 人无感,自选上百后再换窗口函数。

All shipped phases committed and pushed. See git log for atomic per-phase commits. Active plan: `~/.claude/plans/structured-knitting-sutton.md`.

### Phase 1 — what landed

- `Watchlist` model (code PK, name, exchange, added_at). Tables auto-create on startup.
- `app/services/stocks.py`: per-stock `stock_individual_info_em` lookup with `ThreadPoolExecutor(20)` for batch validation. Names cached in-process forever. Format check (`^\d{6}$`) gates the network call.
- Routes: `GET /api/watchlist`, `POST /api/watchlist/import` (returns `{added, skipped_existing, invalid}`), `DELETE /api/watchlist/{code}`.
- Frontend `/watchlist`: table + import modal accepting paste, `.csv`, and `.xlsx` (via `read-excel-file`, first column only).
- Next.js catch-all proxy at `/api/[...path]/route.ts` replaces the bespoke `/api/login` route — all backend calls flow through it, cookies forwarded both directions.

### Phase 2 — what landed

- `Snapshot` model (one row per code per scrape ts) with JSON columns for `signals`, `news`, `notices`, `lhb`. Indexed on `(code, ts)` and `(ts)`.
- `app/services/scraper.py`: per-code akshare collection (Xueqiu spot for price/volume, eastmoney fund-flow, news, notices). LHB pulled separately at the post-close tick. Best-effort: any field can be `None` without breaking the batch; one bad code doesn't sink the others.
- `app/services/signals.py`: 6 baseline rules — `limit_up`/`limit_down` (board-aware: 30/688 = 20%, 60/00 = 10%, 8/4 = 30%), `big_inflow`/`big_outflow` (¥50M threshold), `important_notice` (keyword match: 业绩/重组/减持/…), `lhb`. Strong signals listed in `STRONG_SIGNALS`; UI tints those rows red.
- `app/services/cron.py`: APScheduler in-process, Asia/Shanghai cron at 09:30, 10:30, 11:30, 14:00, 15:00, 16:00 (post-close = LHB pass). Toggle via `SCHEDULER_ENABLED` env (off in tests).
- Routes: `GET /api/stocks` (latest snapshot per watched code, strong signals first), `GET /api/stocks/{code}` (detail), `POST /api/stocks/snapshot?post_close=bool` (manual trigger — useful for testing without waiting for cron).
- Frontend `/stocks`: full table with code/name/price/change%/main flow/signals/news+notice counts/last update. Strong-signal rows tinted; signal chips colored. "手动抓取" button.

### Phase 3 — what landed

- `Analysis` model: cache table, one row per code (replaced on regen). Stores `key_table` JSON, 500-word markdown, snapshot id, model id, strategy name.
- `app/services/strategy.py`: pluggable Strategy registry. MVP ships only `DEFAULT` (no rules → free LLM judgment). To add a strategy: instantiate `Strategy(name=..., rules=["PE<20", ...])`, call `register(s)`, pass `strategy_name` into `generate()`. The prompt builder injects rules as a hard-rules section.
- `app/services/analysis.py`: end-to-end pipeline. System prompt uses Anthropic prompt caching (ephemeral cache_control), so the static strategy block is cheap on subsequent calls. Single tool `submit_analysis` enforces the entire key-table schema + a `deep_analysis` markdown field — model has no choice but to fill the contract.
- Routes: `GET /api/stocks/{code}/analysis` (cached or `null`), `POST` same path (force regen). 4h TTL drives the `is_fresh` flag in the response.
- Default model: `kimi-k2.5` via Aliyun dashscope's coding plan (free tier, see "Analysis model history" below for the why). Overridable via `ANALYSIS_MODEL` env without a code change. Default lives in `DEFAULT_MODEL` in `app/services/analysis.py`.
- Anthropic SDK base URL is configurable via `ANTHROPIC_BASE_URL` env. Production currently points at `https://coding.dashscope.aliyuncs.com/apps/anthropic`; zenmux (`https://zenmux.ai/api/anthropic`) and the official endpoint also work as drop-ins.
- The `tool_choice` call site uses a try/except cascade — strict `{"type":"tool","name":...}` first, falls back to `{"type":"any"}` on a 400. Most providers accept strict; some (MiniMax, GLM, qwen3.5-plus, qwen3.6-plus, DeepSeek-V4) only accept `any`/`auto`. With one tool the two are equivalent.
- Frontend `/stocks/[code]`: empty state with "生成深度解析" CTA when no cache; freshness bar + "重新生成" when cached; key table card (color-coded actionable verdict + 6-row grid); markdown deep-analysis (inline minimal renderer — no markdown library needed for `## headings`, `- lists`, `**bold**`).

### Phase 4 — what landed

- `app/globals.css`: minimal reset + media queries (can't put media queries in inline styles). Tightens padding on small screens, gives tables horizontal scroll via `.table-scroll`.
- Tables in `/stocks` and `/watchlist` wrapped in `.table-scroll` so they don't blow out a 375px viewport.
- PWA: real `manifest.webmanifest` (name, icons, standalone, scope), inline-SVG icon at `/icon.svg`, iOS home-screen meta via `appleWebApp` in `metadata`. "Add to Home Screen" works on both iOS Safari and Android Chrome.
- Middleware exempts `/icon.svg` (same fix shape as `/manifest.webmanifest`).

## Architecture

```
┌──────────────────┐         ┌──────────────────┐         ┌──────────────┐
│  Next.js (App)   │ ─proxy─▶│  FastAPI         │ ──────▶│  Postgres    │
│  - /login        │         │  - /api/auth/*   │         │  watchlist   │
│  - /watchlist    │         │  - /api/watch/*  │         │  snapshots   │
│  - /stocks       │         │  - /api/stocks/* │         │  analyses    │
│  - /stocks/[code]│         │  - APScheduler   │         └──────────────┘
└──────────────────┘         │  - akshare       │
        │                    │  - Anthropic SDK │
        │ middleware checks  └──────────────────┘
        │ rich_session cookie
```

**Why proxy login through Next.js**: keeps the auth cookie same-origin, avoids `SameSite=None; Secure` cross-site cookie config.

**Why one Postgres**: snapshot volume for ≤10 users × ≤500 stocks × 6/day is tiny — no need for TimescaleDB.

## Tech stack

- **Frontend**: Next.js 15 (App Router) + React 19 + TypeScript. Inline styles in MVP (no Tailwind/CSS framework yet — add only if Phase 4 needs it).
- **Backend**: FastAPI + SQLAlchemy 2 + Pydantic v2 + psycopg3. Python 3.11+ recommended (Railway default).
- **DB**: Postgres 16.
- **Scheduler**: APScheduler in-process (Phase 2). For Railway, run with a single backend instance to avoid duplicate jobs.
- **LLM**: `anthropic` SDK, Claude (Opus or Sonnet — TBD in Phase 3 based on cost). Use prompt caching for the per-stock context block.
- **Data**: `akshare` (Python, no API key needed).

## Repo layout

```
rich/
├── CLAUDE.md            ← this file (Claude handoff)
├── README.md            ← user-facing quick start
├── MORNING_NOTES.md     ← 2026-04-27 handoff (delete after morning verification)
├── .env.example         ← all env vars in one place
├── docker-compose.yml   ← local Postgres
├── frontend/
│   ├── package.json
│   ├── middleware.ts    ← auth gate (redirects to /login); /api/* + manifest + icon pass
│   ├── railway.json
│   ├── lib/api.ts       ← thin client for the backend (watchlist, stocks, analysis)
│   ├── app/
│   │   ├── globals.css           ← reset + media queries (mobile padding, .table-scroll)
│   │   ├── layout.tsx            ← metadata + iOS PWA meta
│   │   ├── page.tsx              → redirects to /stocks
│   │   ├── login/page.tsx
│   │   ├── api/[...path]/route.ts ← catch-all proxy → backend, forwards cookies
│   │   ├── watchlist/page.tsx    ← Phase 1: table + import modal
│   │   ├── stocks/page.tsx       ← Phase 2: 盯盘 table with signals + manual trigger
│   │   └── stocks/[code]/page.tsx ← Phase 3: key table + 500-word markdown
│   └── public/
│       ├── manifest.webmanifest  ← PWA manifest
│       └── icon.svg              ← single SVG icon (any/maskable)
└── backend/
    ├── requirements.txt
    ├── Procfile
    ├── railway.json
    └── app/
        ├── main.py             ← FastAPI app + lifespan (create_all + scheduler) + routes
        ├── config.py           ← env via pydantic-settings
        ├── db.py               ← SQLAlchemy engine + session
        ├── auth.py             ← itsdangerous-signed cookie token
        ├── models.py           ← SQLAlchemy: Watchlist, Snapshot, Analysis
        ├── services/
        │   ├── stocks.py       ← akshare lookup + format/exchange detection (Phase 1)
        │   ├── scraper.py      ← per-code akshare collection (Phase 2)
        │   ├── signals.py      ← rule engine over snapshot dicts (Phase 2)
        │   ├── cron.py         ← APScheduler + run_snapshot_job (Phase 2)
        │   ├── strategy.py     ← Strategy registry, MVP only DEFAULT (Phase 3)
        │   └── analysis.py     ← Anthropic tool-use pipeline + cache (Phase 3)
        └── routes/
            ├── auth.py         ← /api/auth/{login,logout,me}
            ├── watchlist.py    ← /api/watchlist (list, import, delete)
            └── stocks.py       ← /api/stocks (list, detail, manual snapshot, analysis)
```

## Diagnostic endpoints

`/api/_diag/*` carries health checks, manual backfills, and one-off
migrations. **Public** (no auth), idempotent unless noted, async when the
work exceeds ~30 s (Railway's HTTP proxy kills longer requests). Full
reference + curl examples in [docs/diag-endpoints.md](docs/diag-endpoints.md)
— check there first before adding a new one; the pattern (sync vs async,
lock conventions, naming) is documented.

Most-frequently-touched:
- `GET /api/_diag/snapshot-schema` — verify the snapshots table columns
- `GET /api/_diag/outcomes-stats` — current LLM hit-rate report
- `GET /api/_diag/outcomes-detail` — why is hit-rate sparse? (raw distribution)
- `GET /api/_diag/nd-outlook-stats` — 次日预判 (看涨/平/跌) vs 真实 d1 收益 (6/10+ anchors only)
- `POST /api/_diag/refresh-financials` (async) — bootstrap or re-pull 财报 data

## Local development

Prereqs: Node 20+, Python 3.11+, Docker (for Postgres).

```bash
# 1. env
cp .env.example .env   # then edit APP_PASSWORD and AUTH_SECRET

# 2. db
docker compose up -d

# 3. backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# 4. frontend (new terminal)
cd frontend
npm install
npm run dev
# → http://localhost:3000
```

Check it's wired up:
- `curl localhost:8000/health` → `{"status":"ok"}`
- Visit `http://localhost:3000` → redirects to `/login` → enter `APP_PASSWORD` → lands on `/stocks`

## Deployment (Railway)

Two services + one Postgres plugin, all from this repo:

1. Create a Railway project, add a **Postgres** plugin. Note its `DATABASE_URL`.
2. Add a service from this repo, **root directory: `backend`**. Set env:
   - `APP_PASSWORD` (shared)
   - `AUTH_SECRET` (`openssl rand -hex 32`)
   - `DATABASE_URL` = the plugin's URL **but switch the scheme to `postgresql+psycopg://`** (SQLAlchemy needs this).
   - `FRONTEND_ORIGIN` = the frontend service's public URL
   - `ANTHROPIC_API_KEY` (Phase 3+)
3. Add a second service from the same repo, **root directory: `frontend`**. Set env:
   - `NEXT_PUBLIC_API_BASE` = the backend service's public URL
4. Both services autodeploy on push to `main`.

Notes:
- In production, change `secure=True` for the cookie in `backend/app/routes/auth.py` (currently `False` for local HTTP).
- The scheduler (Phase 2) must run in exactly one backend instance. Set Railway replicas = 1.

## Conventions for Claude working in this repo

- **Do not add features beyond the current phase.** If the user asks for something off-roadmap, ask whether to add it to the plan or do it now.
- **Update this file** when you finish a phase or make a decision that contradicts the table above.
- **Update the user-facing `README.md`** if commands or env vars change.
- **Don't add a CSS framework** until Phase 4 unless the user asks. Inline styles are fine for placeholders.
- **Don't introduce ORMs/migration tools** until Phase 1 needs them. When you do, use **Alembic** (already implied by SQLAlchemy 2).
- **Commits**: small, scoped, conventional-commit style (`feat(watchlist): ...`, `chore: ...`). Don't squash phases into one commit.
- **Secrets**: never commit `.env`. Always read via `app.config.settings` on the backend, `process.env.NEXT_PUBLIC_*` on the frontend (only `NEXT_PUBLIC_*` is exposed to the browser).
- **Branches**: work on `main` for now (it's a tiny team). If we ever add CI, switch to PR flow.

## Known environmental gotchas

- **akshare + local HTTPS proxy**: some Mac users run Clash/V2Ray on `127.0.0.1:7897` for international traffic; that proxy can drop connections to `*.eastmoney.com` (Chinese host). On Railway (Linux container, no proxy), akshare works without issue. If you need to validate akshare locally, either disable the proxy for the eastmoney host or test through Railway.
- **Python version**: backend requires Python 3.11+ (psycopg3 binary wheels start there). macOS system Python 3.9 will fail at `pip install`.
- **Railway ~30s HTTP proxy timeout vs slow LLM gateways**: Railway's edge proxy kills any HTTP request past ~30s. A single deep analysis on a slow gateway (e.g. 6/28 temporary switch zenmux→`minimax/minimax-m3`, measured ~50s for a full ~3000-token output) blows past that, so a *synchronous* `POST /{code}/analysis` returned "Failed to fetch" to the browser even though the LLM call kept running and eventually persisted its row. **Fixed 6/28 by making the detail-page generate/regenerate endpoint async**: the POST fires a daemon thread (own `SessionLocal`) and returns `{started}` immediately; the frontend polls `GET /{code}/analysis/status` (`{running, error}`) and re-fetches `GET /{code}/analysis` once running flips false. Per-code guard `_single_analysis_jobs` in routes/stocks.py mirrors the batch pattern. The daily cron is unaffected — it already runs in a background thread (180s budget), not bound by the HTTP proxy. (Note: the local Clash/V2Ray proxy on `127.0.0.1:7897` ALSO caps long connections at ~30s, so curl-testing a gateway locally needs `--noproxy '*'` to get the true latency.)
- **Long jobs in daemon threads + Railway**: every push triggers a redeploy → SIGTERM to the running container → daemon threads die. snapshot/analysis jobs are designed to be SIGTERM-resilient via per-row commit, but historically had a bug where `collect_many` did all the slow work *before* the commit loop, so a SIGTERM during that phase lost everything. Fixed 4/29 by switching to per-worker commit (`_snapshot_worker` in cron.py) — each worker commits its own row immediately on success.
- **DB pool sizing for parallel workers**: snapshot job runs 10 worker threads in parallel. Holding a DB session across the slow akshare phase saturated the default pool (5 + 10 overflow = 15 max), silently failing every commit. Fixed by (a) bumping pool_size to 20 + max_overflow=20, (b) opening Session AFTER the akshare phase so connections are held only for the millisecond-long write. See `_snapshot_worker` and `engine` config in db.py.
- **akshare default timeout is 30s per call**: with N workers × 3 fan-out endpoints, a single bad eastmoney route parks a worker for 30s. We monkey-patch `requests.Session.send` in `services/__init__.py` to a 12s default. Caller-supplied timeouts still win.
- **akshare tqdm noise**: akshare writes per-call progress bars to stderr. uvicorn tags everything on stderr as `[err]`, so a healthy job looks catastrophic. We disable tqdm globally in main.py before any akshare import (`TQDM_DISABLE=1` env + monkey-patch).
- **Diagnostic endpoint**: `/api/_diag/snapshot-schema` returns the live snapshots table column list + which expected extras are missing. Auth-disabled for now; useful when debugging deploys without Railway shell access.

## Analysis model history

| Date | Default model | Gateway | Why switched |
|---|---|---|---|
| Phase 3 (~4/26) | `claude-sonnet-4-6` | zenmux | Initial choice — matched user's "克制研究员" tone preference. |
| 4/27 | (evaluated DeepSeek V4-pro/flash) | zenmux | DeepSeek too slow (98s/call), no prompt cache, tool_choice rejected. Decided not to adopt. |
| **4/28** | **`kimi-k2.5`** | **dashscope coding plan** | **Sonnet quota exhausted.** Benchmarked 7 dashscope models on 300638 (a hard case: 91% earnings drop + cash flow crisis). kimi-k2.5 won on speed (25s, fastest reliable), structured-output reliability (5 red_flags, most thorough), and was the only fast model that supports the strict `tool_choice={"type":"tool",...}` shape — zero protocol changes needed. |
| 6/28 (temp) | `minimax/minimax-m3` | zenmux (`https://zenmux.ai/api/anthropic`) | **火山 ARK provider degraded** — temporary failover. zenmux+minimax-m3 verified working (strict tool_choice OK), but slower end-to-end (~50s for a full analysis vs 火山's ~22s). That latency is what forced the **async-analysis fix** (see gotchas) since it exceeds Railway's 30s proxy. |
| **6/29** | **`anthropic/claude-haiku-4.5`** | **zenmux** | **Balanced pick** after a 6-model bench on zenmux (easy 茅台 case + hard 业绩塌方 case, real `submit_analysis` schema + strict tool_choice). haiku-4.5 = fastest viable (~33s, 35% under minimax-m3's ~50s), native strict tool_choice, caught the major hard-case red_flags (8: 商誉/审计/现金流/减持). Tradeoffs: on a clearly-bad stock its verdict is one notch softer (不建议入手 vs minimax/sonnet's 建议卖出), and prose is shorter (~1200 字). Quality ceiling was sonnet-4.6 (decisive 建议卖出, 12 flags) but ~58s doesn't fix the speed pain; kimi-k2.6 (105s) + deepseek-v4-flash (no strict tool_choice, broken key_table) disqualified; glm-4.7 fast but weakest output. **Watch**: sell-line is the active main line and haiku is softest exactly on sell verdicts — revisit if sell sharpness suffers. Revert to 火山 when it recovers. |
| **~6月底 (止血,当前生产默认)** | **`MiniMax-M2.5`** | (Rush 手动切,env 变更未记录在此) | Rush 止血时切换,线上情况她掌握。7/2 从生产分析行观察确认 model=MiniMax-M2.5。**注意**:该模型不严格遵守 tool schema 的 required(603986 曾吐 sell_price null 引发详情页崩溃),已在 `_validate_price_fields` validator + 前端 null 防护双层拦截。 |

Other dashscope candidates from 4/28 testing, ordered by viability:
- **MiniMax-M2.5** (25s, 4 red_flags) — tied for speed, but only `any`/`auto` tool_choice. Solid backup.
- **glm-4.7** (28s, 4 red_flags) — slightly slower, also `auto` only.
- **qwen3-max-2026-01-23** (41s, **2 red_flags — missed cash flow + insider trading**) — strong tool support but worst structured-output quality on this case.
- **glm-5** (50s, 3 red_flags) — slow.
- **qwen3.5-plus / qwen3.6-plus** (78s / 117s) — reasoners, way too slow for batch (47 stocks × 100s ≈ 78 min).

Switching back to Sonnet (when quota restores) is one env-var change: `ANALYSIS_MODEL=claude-sonnet-4-6` and `ANTHROPIC_BASE_URL=https://zenmux.ai/api/anthropic`.

## Done — and what's next-level work

The MVP is complete. Phase 5 (technical indicators) is queued — see the phase plan above. Other future iterations:

- **Snapshot history UI**: snapshots are stored but not visible. A timeline view per stock would surface intra-day evolution. Spec was explicit about deferring this.
- **第三层深度解析**: research-report-length analysis (~2000 words) on demand. Same pipeline, longer max_tokens, different prompt.
- **Strategy authoring UI**: today strategies are code-defined. Adding a CRUD UI for strategy rules would let the user iterate without redeploys.
- **北向资金 per-stock**: skipped in MVP for simplicity. `ak.stock_hsgt_hold_stock_em` is the entry point; integrate into `scraper.py`.
- **Snapshot retention**: nothing prunes old rows. At 5min ticks during trading hours × 100 stocks × 365 days = ~870k rows/yr (with the new tier) — fine for years, but a cron-based pruner is one-day work when needed.
- **LongBridge integration** (v3+): if any user has a LongBridge live account, their position data could auto-fill the `scenario_advice` 4-quadrant (未持/大赚/小赚亏/大亏) without the user self-reporting.
