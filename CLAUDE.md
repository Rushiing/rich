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

All phases committed and pushed. See git log for atomic per-phase commits.

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
- Default model: `claude-sonnet-4-6`. Override via the `MODEL` constant in `app/services/analysis.py`.
- Anthropic SDK base URL is configurable via `ANTHROPIC_BASE_URL` env (the user routes through `https://zenmux.ai/api/anthropic`). Empty value = official endpoint.
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

## Done — and what's next-level work

The MVP is complete. Future iterations could include:

- **More signals**: the spec called for 放量突破20日高 / 跌破20日均线 / 北向加仓 — these need historical K-line data (an extra akshare call per code per cron tick). Add to `services/scraper.py` + `services/signals.py` when the appetite is there.
- **Snapshot history UI**: snapshots are stored but not visible. A timeline view per stock would surface intra-day evolution. Spec was explicit about deferring this.
- **第三层深度解析**: research-report-length analysis (~2000 words) on demand. Same pipeline, longer max_tokens, different prompt.
- **Strategy authoring UI**: today strategies are code-defined. Adding a CRUD UI for strategy rules would let the user iterate without redeploys.
- **北向资金 per-stock**: skipped in MVP for simplicity. `ak.stock_hsgt_hold_stock_em` is the entry point; integrate into `scraper.py`.
- **Snapshot retention**: nothing prunes old rows. At 6 ticks/day × 100 stocks × 365 days = ~220k rows/yr — fine for years, but a cron-based pruner is one-day work when needed.
