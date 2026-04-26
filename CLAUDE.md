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

## Phase plan (~6 working days total)

- [x] **Phase 0** — Skeleton: Next.js + FastAPI + Postgres + single-password login
- [x] **Phase 1** — Watchlist CRUD + paste/Excel import + akshare code validation
- [ ] **Phase 2** — APScheduler + 6 akshare endpoints + signals engine + 盯盘 view (~2 days)
- [ ] **Phase 3** — Prompt template + strategy slot + Claude tool use key table + 500-word markdown + 4h cache (~2 days)
- [ ] **Phase 4** — Mobile responsive pass + PWA install (~0.5 day)

When you finish a phase, check the box above and add a one-line summary of what landed.

### Phase 1 — what landed

- `Watchlist` model (code PK, name, exchange, added_at). Tables auto-create on startup.
- `app/services/stocks.py`: per-stock `stock_individual_info_em` lookup with `ThreadPoolExecutor(20)` for batch validation. Names cached in-process forever. Format check (`^\d{6}$`) gates the network call.
- Routes: `GET /api/watchlist`, `POST /api/watchlist/import` (returns `{added, skipped_existing, invalid}`), `DELETE /api/watchlist/{code}`.
- Frontend `/watchlist`: table + import modal accepting paste, `.csv`, and `.xlsx` (via `read-excel-file`, first column only).
- Next.js catch-all proxy at `/api/[...path]/route.ts` replaces the bespoke `/api/login` route — all backend calls flow through it, cookies forwarded both directions.

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
├── .env.example         ← all env vars in one place
├── docker-compose.yml   ← local Postgres
├── frontend/
│   ├── package.json
│   ├── middleware.ts    ← auth gate (redirects to /login); /api/* always passes
│   ├── railway.json
│   ├── lib/api.ts       ← thin client for the backend
│   ├── app/
│   │   ├── layout.tsx
│   │   ├── page.tsx              → redirects to /stocks
│   │   ├── login/page.tsx
│   │   ├── api/[...path]/route.ts ← catch-all proxy → backend, forwards cookies
│   │   ├── stocks/page.tsx       (placeholder until Phase 2)
│   │   └── watchlist/page.tsx    (Phase 1: table + import modal)
│   └── public/manifest.webmanifest
└── backend/
    ├── requirements.txt
    ├── Procfile
    ├── railway.json
    └── app/
        ├── main.py             ← FastAPI app + lifespan (create_all) + routes
        ├── config.py           ← env via pydantic-settings
        ├── db.py               ← SQLAlchemy engine + session
        ├── auth.py             ← itsdangerous-signed cookie token
        ├── models.py           ← SQLAlchemy ORM models (Watchlist, ...)
        ├── services/
        │   └── stocks.py       ← akshare lookup + format/exchange detection
        └── routes/
            ├── auth.py         ← /api/auth/{login,logout,me}
            └── watchlist.py    ← /api/watchlist (list, import, delete)
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

## Open questions (TBD in their phases)

- **Phase 2**: which 5–8 baseline signals? Suggested: 涨停/跌停, 放量突破20日高, 跌破20日均线, 主力净流入Top, 北向加仓, 上龙虎榜, 重要公告（业绩/重组/减持）.
- **Phase 3**: Claude model — Opus for quality vs Sonnet for cost. Default to Sonnet 4.6 first; promote to Opus if quality is insufficient.
- **Phase 3**: prompt cache strategy — cache the static part (策略框架 + 解析模板), pass the per-stock snapshot as the variable suffix.
