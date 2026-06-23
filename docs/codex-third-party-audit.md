# Codex Third-Party Audit

Date: 2026-06-23  
Reviewer: codex  
Repository: `Rushiing/rich`

## Executive Summary

This project is not a fragile prototype. The core architecture is coherent for a small internal A-share monitoring product: FastAPI + Next.js is a reasonable split, the data ingestion pipeline has meaningful fault tolerance, LLM analysis is cached and structured, and the product has already evolved from a watchlist tool into a measured decision-support system with outcome tracking, a virtual candidate pool, and a first pass at claim auditing.

The highest-risk issues are not basic code quality problems. They are:

1. **Production safety boundary is too loose.** Public diagnostic and evaluation endpoints expose capabilities that can read logs, trigger long-running jobs, reset evaluation outputs, refresh data, and potentially consume external API/LLM quota.
2. **A core outcome metric uses the wrong return basis.** The code records `anchor_close` as the dividend-safe basis, but still computes `return_dN` from unadjusted `anchor_price`. This affects the product's strongest customer claim: historical buy hit-rate and excess return.
3. **Some effect claims are being displayed more formally than their validation supports.** `risk_scores`, next-day target intervals, sell-side confidence, and some time-window validity claims need to be downgraded or explicitly labeled until measured.
4. **User-scoped and global operations are partially mixed.** The app now has per-user watchlists and holdings, but some batch analysis paths still operate globally.

Overall judgment: the system is worth continuing. It needs a short hardening pass before further product expansion, especially if more users or public URLs are involved.

## Review Scope

I reviewed the repository from three angles:

- **System stability and security:** backend routes, auth/session handling, DB models, background jobs, cron jobs, data ingestion, public diagnostic endpoints, and frontend/backend proxy behavior.
- **Product/effect quality:** how analysis claims are generated, displayed, cached, measured, and audited; whether UI wording matches available evidence.
- **Claude self-audit follow-up:** after completing the independent review, I read `docs/customer-claim-audit.md` and compared its rubric/scoring with the codebase.

I did not modify application code in this audit. I ran:

- `frontend`: `npm run typecheck`
- `backend`: `.venv/bin/python -m compileall app`

Both passed. I did not find a test suite directory, so no unit or integration tests were run.

## Repository and Product Understanding

The product is an internal A-share monitoring and analysis tool for a small trusted group.

Core loop:

1. Users maintain a watchlist of A-share codes.
2. Backend scrapes market snapshots, fund flows, news, announcements, technical data, sector context, and related signals.
3. UI provides a watchlist/monitoring view, stock detail pages, holdings-aware action items, sector pages, and a virtual candidate pool.
4. LLM analysis generates a structured key table and markdown analysis, cached and later evaluated against market outcomes.
5. The product has started moving from raw recommendations toward measured claims: hit-rate, excess return, deduped stats, next-day outlook stats, price-level outcome stats, and replay eval tooling.

This is a good direction. The main problem is that measurement and safety have not fully caught up with the product's increasing scope.

## Critical Findings

### P0: Public Diagnostic and Evaluation Surface Is Too Powerful

Evidence:

- `docs/diag-endpoints.md` states that `/api/_diag/*` is public and that production has `AUTH_DISABLED=true`.
- `backend/app/routes/eval.py` explicitly declares evaluation endpoints as intentionally unauthenticated.
- The eval route can start long-running subprocess work, expose debug logs, stop jobs, and delete evaluation outputs.

Impact:

- Anyone with the URL can potentially trigger expensive jobs, consume LLM/model quota, inspect diagnostic output, or delete evaluation artifacts.
- Public unauthenticated diagnostic endpoints also create a future footgun: every new debug endpoint inherits the risk unless protected by default.
- This is especially dangerous because the product handles financial decision support and external API keys.

Recommendation:

1. Turn off production `AUTH_DISABLED`.
2. Protect all `/api/_diag/*` and `/api/_eval/*` endpoints behind an admin-only guard.
3. Add a server-side `ENABLE_DIAG_ENDPOINTS` / `ENABLE_EVAL_ENDPOINTS` switch defaulting to false in production.
4. Keep destructive endpoints behind both auth and an explicit confirmation parameter.
5. Document the production policy in `docs/diag-endpoints.md`.

### P0: Committed Runbook Contains a Likely Real API Key

Evidence:

- `docs/replay-eval-runbook.md` contains a concrete-looking `VOLCENGINE_API_KEY` value.

Impact:

- Even if the key is expired or low-privilege, it must be treated as leaked.
- If the repo has ever been pushed to a remote accessible by others, deleting the line in a later commit is not enough.

Recommendation:

1. Revoke/rotate the key immediately.
2. Replace the runbook value with a placeholder.
3. Consider secret scanning history if the repository was public or shared.
4. Add a local/pre-push secret scanning habit for runbooks and `.md` files, not only `.env`.

### P1: Outcome Returns Still Use `anchor_price`, Not Dividend-Safe `anchor_close`

Evidence:

- `AnalysisOutcome.anchor_close` is documented as the qfq close of the anchor day and intended as the dividend-safe basis.
- `backfill_outcomes()` still computes `return_d1`, `return_d3`, and `return_d5` from `o.anchor_price`.
- `hit_rate_stats()` consumes those persisted `return_d5` values.

Impact:

- The core customer claim around buy hit-rate and excess return may be distorted, especially around dividend/adjustment events.
- This does not prove the reported alpha is false. It means the metric implementation does not yet match the intended measurement contract.
- Until recomputed, I would not externally claim the current buy hit-rate/excess-return numbers as fully audited.

Recommendation:

1. Use `anchor_close` as the preferred return basis when available.
2. Recompute historical `return_dN` values, or introduce clearly named adjusted return fields.
3. Re-run `hit_rate_stats`, `outcomes-detail`, customer claim audit, and any UI summary after recomputation.
4. Add a small regression test for one synthetic outcome where `anchor_price != anchor_close`.

### P1: User-Scoped UI Can Trigger Global Batch Analysis

Evidence:

- `POST /api/stocks/analysis/batch` starts a background job.
- That job calls `run_daily_analysis_job()`.
- `run_daily_analysis_job()` queries all distinct watchlist codes globally.
- `analysis.generate()` also selects the first `Watchlist` row for a code without owner scope.

Impact:

- A normal user action can refresh analyses for all users' watchlist codes.
- This can consume LLM quota and overwrite the global analysis cache.
- It also weakens the mental model created by the account system: some surfaces are per-user, while batch analysis is still global.

Recommendation:

1. Split user-scoped batch analysis from admin/global batch analysis.
2. For user-triggered batch jobs, resolve the current owner and analyze only that owner's watchlist.
3. Keep global daily analysis as an admin or cron-only path.
4. Update frontend copy if a button intentionally runs a global job.

### P1: Global Analysis Cache Does Not Encode User Context

Evidence:

- `Analysis` is keyed by code, not by user.
- Holdings-aware UI exists separately, but the generated analysis cache itself is global.

Impact:

- This is acceptable if analysis is deliberately position-agnostic and holdings advice is only computed/displayed separately.
- The risk grows if future prompts include user-specific cost basis, holdings, or preferences. A global cache would then leak or mix user-specific reasoning.

Recommendation:

- Keep current global cache only for market/company analysis.
- If holdings or user preferences ever enter the prompt, create a separate per-user analysis artifact or explicitly exclude those fields from cached LLM input.

## High-Priority Findings

### P2: Invite Code Consumption Is Not Actually Atomic

Evidence:

- Registration checks invite usage, creates the user, increments `current_uses`, and commits.
- There is no row lock or conditional update guarding concurrent registration.

Impact:

- Under concurrent registration, an invite code can exceed `max_uses`.
- Low probability for a small internal group, but the code comment says atomic and the implementation is not.

Recommendation:

- Use `SELECT ... FOR UPDATE` in the transaction, or perform a conditional `UPDATE invite_codes SET current_uses = current_uses + 1 WHERE ... RETURNING`.

### P2: Production Cookie Security Is Still Hard-Coded for Local HTTP

Evidence:

- Session cookies are set with `secure=False`.

Impact:

- Fine for local HTTP.
- Wrong for production HTTPS.

Recommendation:

- Make cookie `secure` environment-aware.
- Use `secure=True` in production.

### P2: Auth-Disabled Mode Has Ambiguous Data Semantics

Evidence:

- `require_auth()` can return `None` when `AUTH_DISABLED=true`.
- Some routes interpret no owner as admin/legacy/global.
- `resolve_owner(None, db)` may return admin or `None`, and callers can treat `None` as unscoped.

Impact:

- This is workable for migrations and local development, but it is dangerous in production because it collapses user scoping.
- It makes route behavior harder to reason about: some endpoints are strict, others become global.

Recommendation:

- Treat `AUTH_DISABLED` as local-only.
- In production, require a user for all user-data routes.
- If admin/global access is needed, make it explicit through an admin identity rather than `None`.

### P2: Long-Running Background Jobs Are Thread-Based and Process-Local

Evidence:

- Snapshot jobs, analysis batch jobs, pool tick jobs, and eval jobs are started in-process.
- Some are protected by locks/status dictionaries.

Impact:

- This is acceptable for a single Railway backend instance, as documented.
- It remains vulnerable to deploy restarts and cannot coordinate across multiple replicas.

Recommendation:

- Keep replicas at one while using this architecture.
- If user count or job volume grows, move jobs to a durable queue or explicit worker process.
- For now, add a short operational note near every route that starts a background job.

## Product and Effect Audit

### Strong Areas

The project has unusually good measurement instincts for an MVP:

- Deduped hit-rate avoids repeated cron-anchor inflation.
- Excess return against same-day baseline is better than raw hit-rate.
- Outcome anchors preserve model, next-day outlook, confidence, and anchor close.
- Replay eval tooling indicates the team is trying to compare model behavior, not just ship a prompt.
- The virtual preselection pool is conceptually sound: candidates are observed before being recommended, and elimination rules are machine-checkable.
- The detail page was reorganized around decision order, not content order.

These choices should be preserved.

### Current Claim Ratings

Using the rubric in `docs/customer-claim-audit.md`, my second-pass ratings are:

| Claim / Surface | Current rating | Judgment |
|---|---:|---|
| Buy-side historical hit-rate / excess return | E3, not yet E4 | Promising measured evidence, but blocked by `anchor_price` vs `anchor_close` return-basis bug. |
| Sell-side historical performance | E2/E3 | There is measurement, but Claude's own audit says sell excess is near zero. Should not be marketed as alpha. |
| Next-day trend direction | E3 | There is outcome scoring for trend, but bearish hit-rate and confidence calibration are weak. |
| Next-day target price interval | E1/E2 | Displayed in UI, but I did not find equivalent interval-hit validation. |
| LLM confidence | E2 | Buy confidence seems partially useful; sell confidence appears unreliable. Needs direction-specific display rules. |
| `risk_scores.overall` | E1 | Generated and displayed, but not validated against outcomes. |
| `data_completeness` | C1 | Displayed as a footnote. Low risk, but wording should clarify it means input completeness, not forecast accuracy. |
| `valid_window` | E1/E2 | Only simple machine-checkable forms are parsed. Event-based windows are skipped, so product wording must avoid false precision. |
| Price levels: buy/sell/stop | E2 | Price-level stats exist, but this is not yet mature enough for strong claims. |
| Virtual pool lifecycle | C1/E2 | Product logic is coherent, but realized recommendation quality still needs longitudinal data. |

### Difference From Claude's First Audit

I largely agree with Claude's risk list:

- `risk_scores` should be downgraded.
- Sell confidence should be treated as unsafe until calibrated.
- Bearish next-day outlook is weak.
- `valid_window` should only be trusted when machine-checkable.
- Price levels are a user-experience feature before they are a validated prediction surface.

My main disagreement is about the strongest positive claim:

> Claude marked the buy-side hit-rate/excess-return claim as highly defensible.

I would temporarily downgrade it because the return computation still uses `anchor_price`. Once adjusted returns are recomputed from `anchor_close`, the claim may become E4 again. But it should not be treated as fully audited before that fix.

### UI Wording Risks

The product is generally honest in tone, but several labels can imply more certainty than the system has earned:

- "综合评级" for `risk_scores.overall` sounds authoritative.
- "置信度 高/中/低" on next-day outlook is not well calibrated across directions.
- "目标区间" for next-day outlook looks precise even though interval accuracy is not separately shown.
- "输入完整度" is acceptable but should be clearly separated from "判断准确率".

Suggested safer wording:

- `综合评级` -> `模型综合判断`
- `置信度` -> `模型自评置信度`
- `目标区间` -> `模型预估区间`
- `输入完整度` -> `输入材料完整度`

## Stability Assessment

### Data Ingestion

The ingestion path is generally robust for MVP scale:

- Per-code failures do not sink whole batches.
- Worker-level commits reduce data loss on partial failures.
- Carry-forward quotes protect the UI from blank rows.
- Timeouts and pool sizing gotchas are documented.
- Scheduler is explicitly single-instance.

Main residual risks:

- External data endpoints can block or change without warning.
- Some diagnostic/manual jobs can overlap with scheduled jobs.
- Long-running jobs rely on process lifetime.

### Database and Migrations

The project uses SQLAlchemy models plus manual DDL/`create_all` style migration helpers. This is acceptable for an MVP, but the schema has grown enough that migration drift risk is now real.

Recommendation:

- Do not introduce a migration framework casually in the middle of an urgent fix.
- But before the next major feature phase, consider a small Alembic baseline or at least a formal schema migration checklist.

### Frontend

Frontend typecheck passes. The UI is mostly resilient:

- It handles loading/empty/error states.
- It keeps stale data visible in some failure modes.
- It exposes analysis freshness and history.
- It has responsive structures and avoids heavy frontend dependencies.

Notable issue:

- Batch analysis timeout constant is 20 minutes, but the message says 10 minutes.

## Recommended Fix Order

### Immediate Hardening

1. Revoke/rotate the exposed Volcengine key.
2. Remove the key from the runbook and replace with a placeholder.
3. Disable production `AUTH_DISABLED`.
4. Gate `/api/_diag/*` and `/api/_eval/*` behind admin auth.
5. Make cookie `secure=True` in production.

### Measurement Correction

1. Fix outcome return computation to use `anchor_close`.
2. Backfill/recompute historical returns.
3. Re-run hit-rate summary, outcome detail, next-day stats, and customer claim audit.
4. Update `docs/customer-claim-audit.md` with revised scores.

### Scope and Cost Control

1. Split user-triggered batch analysis from global daily analysis.
2. Add owner scoping to user-triggered batch jobs.
3. Make global batch analysis admin-only.

### Effect Wording

1. Downgrade or relabel `risk_scores`.
2. Relabel next-day confidence as model self-confidence.
3. Avoid strong public claims for next-day target intervals until interval scoring exists.
4. Keep buy-side alpha claims internal until adjusted-return recomputation confirms them.

### Later Engineering Hygiene

1. Add tests around outcome computation.
2. Add tests around auth-disabled and owner-scoped route behavior.
3. Add a migration discipline before the next major schema phase.
4. Consider durable job infrastructure only when user count or workload justifies it.

## Notes for Claude

Please treat this audit as a second-pass review, not a rejection of the current architecture. The project has a strong foundation. The main ask is to tighten the boundary between:

- internal diagnostic power vs public production surface;
- measured evidence vs generated model wording;
- user-scoped behavior vs global maintenance jobs;
- raw model output vs product claims.

The fastest path to a safer product is not a broad refactor. It is a narrow sequence:

1. secure the public/admin boundary;
2. rotate the exposed key;
3. correct the outcome return basis;
4. re-run the claim audit;
5. adjust UI wording where validation is weak.

After that, the product can continue evolving without carrying avoidable trust debt.
