# Steward: an AI that participates in your finances, inside limits you set

**Hackathon theme:** AI as a Financial Participant · **Topic:** Personal Finance Assistant (smart budgeting, bill analysis, spending insights)

Most finance apps stop at charts. Steward is an AI agent that **acts**: it sets budgets, audits bills line by line, pays clean bills before they're late, sweeps spare cash into savings, disputes duplicate charges and cancels price-hiked subscriptions. It never acts beyond the **permission tier** the user grants. Every action passes a deterministic **policy engine** and is written to a **tamper-evident audit log**. The prototype runs on a **simulated core-banking sandbox**, so no real money moves.

> **Repository:** `https://github.com/<your-org>/steward-finance` (replace once pushed)  ·  **Team:** _add names_

---

## Why it matters

| Everyday problem | What Steward does |
|---|---|
| Late fees and forgotten bills | Audits each statement, then pays clean bills to allowlisted billers before the due date (Tier 3), or prepares them for one-tap approval (Tier 2). |
| Bills quietly creep up ("VAS" add-ons, roaming, usage spikes, GST errors) | Line-item **bill audit**: arithmetic, 9% GST check, fees, new charges vs last bill, spikes vs history. Flagged bills are never auto-paid. |
| Subscriptions creep and price hikes | Detects recurring charges, cadence, annual cost, **price hikes**, and **overlapping** services; proposes cancellation. |
| Budgets nobody keeps | Budgets from your *median* month, 50/30/20 check, and a **month-end projection** that adds known upcoming bills to your spending pace. |
| "Can I afford this?" | Probabilistic **cash-flow forecast** (P10–P90 band, overdraft probability) and affordability check against your safety floor. |
| Savings that never happen | **Safe-to-save** sweep: moves only money the pessimistic forecast says you won't need, within your limits. |
| Trusting an AI with money | **Tiers 0–3**, hard limits, balance floor, step-up re-authentication, kill switch, idempotent actions, hash-chained audit log, prompt-injection quarantine. |

## Features

- **Dashboard:** balances, month-to-date spend, safe-to-spend until payday, 30-day forecast chart with an uncertainty band, a financial health score (0–100, six explained components), ranked insights with one-click "Ask Steward" actions, and bills to pay.
- **Spending insights:** 6-month category trend, category donut, top merchants, month-over-month changes, and a **weekly digest** written by Claude. The digest is checked so that every number in the AI text must come from the engines (grounded generation).
- **Transactions:** search and filter, recategorise (the categorizer learns the merchant), **CSV statement import** with column auto-detection and duplicate protection, and CSV export with formula-injection protection.
- **Bills & subscriptions:** open statements with audit verdicts, a paste-any-bill **analyzer**, subscription table (annual cost, price change, sparkline), recurring bills, income streams, and the biller allowlist.
- **Budgets & goals:** projected budget status, suggestions with a 50/30/20 check, savings goals with required monthly amount and projected completion, and an affordability checker.
- **Agent:** chat with tool use (Claude, with an offline fallback), an approval queue showing every policy check, tier selection, limits, the kill switch, and an activity history.
- **Audit trail:** every login, policy change, proposal, decision and execution, hash-chained. One click verifies both the chain and the ledger.
- **Sandbox:** fast-forward 1, 7 or 30 days. Salary, spending, subscriptions and bills post day by day, overdue bills earn late charges, and the agent runs its daily cycle under your tier.

## Permission tiers (the core idea)

| Risk class | Example actions | T0 Observer | T1 Organizer | T2 Assistant | T3 Autopilot |
|---|---|---|---|---|---|
| R1 non-monetary | set budget, recategorise, reminder | deny | **auto** | **auto** | **auto** |
| R2 own accounts | sweep to savings | deny | approve | approve | **auto within limits** |
| R3 third parties | pay bill · cancel subscription · dispute | deny | deny | approve | **pay clean allowlisted bills only**; others approve |
| R4 prohibited | add payee, pay people, trade, change own limits | never | never | never | never |

Auto-execution also requires: kill switch off, ownership checks, per-action, daily and monthly limits, the balance floor, an S$10,000 hard ceiling, a statement-exact amount, a clean bill audit, and a velocity cap. Any failed soft check falls back to human approval. Any failed hard check denies the action. Approved actions are **re-checked at execution time**. Details: [docs/SECURITY_SELF_ASSESSMENT.md](docs/SECURITY_SELF_ASSESSMENT.md).

## Quick start

Requirements: Python 3.11+ (tested on 3.11–3.14). No Node.js, no build step.

```bash
python -m venv .venv
```
```bash
.venv/Scripts/python -m pip install -r requirements-dev.txt      # Windows (use .venv/bin/python on macOS/Linux)
```
```bash
.venv/Scripts/python -m uvicorn steward.asgi:app --port 8000
```

Open http://localhost:8000, create an account (keep "Load 6 months of sandbox banking data" ticked) and explore. New accounts start at **Tier 1**. Raise the tier on the **Agent** page; it asks for your password again.

**Turn on Claude (optional):** copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY`. The default model is `claude-opus-5-5` with low effort for chat. Without a key, Steward runs a deterministic offline agent over the same tools, so the whole product (and CI) works with no network.

### Docker (API + PostgreSQL)

```bash
docker compose up --build
```

The API is served at http://localhost:8000 with PostgreSQL storage. Set `COOKIE_SECURE=true` and `STEWARD_ENV=production` behind HTTPS.

### Configuration

All settings come from environment variables (see [.env.example](.env.example)): `DATABASE_URL`, `STEWARD_LLM_PROVIDER` (`auto` | `anthropic` | `offline`), `STEWARD_LLM_MODEL`, `STEWARD_LLM_EFFORT`, `COOKIE_SECURE`, `SESSION_IDLE_MINUTES`, `SESSION_ABSOLUTE_HOURS`, `STEP_UP_MINUTES`, `ALLOW_REGISTRATION`, `STEWARD_CURRENCY`.

## Tests

```bash
.venv/Scripts/python -m pytest
```

There are **222 tests** (about 35 s). They make no network calls and use a temporary database per test. Coverage:

- Engines: money, merchant normalisation, the categorizer cascade and Naive Bayes, recurring detection and price hikes, anomalies, forecast and affordability, budgets, health score, goals, bill audit, and the CSV importer.
- The **full tier × action policy matrix**, every prohibited action at every tier, limits, the floor, the hard ceiling, velocity, and approval re-checks.
- Security primitives: password hashing, prompt-injection detection, PII redaction, the rate limiter, and audit-chain tamper detection (edited, deleted and rehashed entries).
- HTTP security: cookies, CSRF, session expiry, step-up, brute-force lockout, tenant isolation, and validation.
- End-to-end agent flows: Tier 0–3 behaviour, exactly-once approval, step-up for large approvals, the time-of-check/time-of-use re-check, the kill switch, disputes, cancellations, late fees, a 45-day simulation with integrity checks, offline chat, PII redaction, a **compromised-model attack**, and CSV import.

Lint: `ruff check steward tests scripts`. CI runs both on Python 3.11, 3.12 and 3.13, builds the Docker image, and uploads fresh execution evidence ([.github/workflows/ci.yml](.github/workflows/ci.yml)).

## Execution evidence (sandbox operation logs)

```bash
.venv/Scripts/python scripts/run_demo_scenario.py
```

This drives the real app through onboarding, then Tier 0, 1, 2 and 3. It simulates a 30-day Autopilot month, runs adversarial attacks (a compromised LLM, direct API abuse, a missing CSRF token, missing re-authentication, cross-tenant access, brute force, the kill switch), and finishes with integrity checks, including a tamper test on a copy of the database. Outputs go to [evidence/](evidence):

| File | Contents |
|---|---|
| [EXECUTION_EVIDENCE.md](evidence/EXECUTION_EVIDENCE.md) | Human-readable summary of what happened and why |
| `sandbox_operation_log.jsonl` | Every API call (step, request with secrets redacted, status, response digest) |
| `agent_actions.json` | Every agent action with its policy decision, reasons and per-check results |
| `audit_log.jsonl` | The hash-chained audit trail export |
| `integrity_verification.json` | Audit-chain and ledger verification, plus tamper-test result |

## Project layout

```
steward/
  core/        pure analytics engines (no I/O): categorizer, recurring, anomalies, forecast,
               budgeting, health, goals, bill_audit, importer, insights, money, merchant
  agent/       policy engine (tiers & guardrails), action schemas, action lifecycle service,
               daily cycle planner, tools, LLM providers (Claude / offline / scripted), chat, digest
  security/    scrypt passwords, sessions + CSRF + step-up, rate limiter, untrusted-text handling,
               hash-chained audit log
  sandbox/     simulated core-banking ledger, synthetic persona, day-by-day simulator
  services/    Analysis: loads a user's data once and runs the engines
  api/         FastAPI routers (auth, finance, agent/audit/sandbox)
  main.py      app factory (security headers, error handling, static UI)
web/           dependency-free single-page app (ES modules, SVG charts, strict CSP)
tests/         222 pytest tests
scripts/       run_demo_scenario.py (execution evidence)
docs/          technical documentation, security self-assessment, demo video script, pitch notes
evidence/      generated sandbox operation logs and verification results
```

API docs (development mode): http://localhost:8000/api/docs, 50 endpoints under `/api/v1`.

## Submission deliverables

| Deliverable | Where |
|---|---|
| 1. Technical documentation | [docs/TECHNICAL_DOCUMENTATION.md](docs/TECHNICAL_DOCUMENTATION.md) |
| 2. Presentation deck (≤10 min) | [Slide deck](https://claude.ai/artifact/VKjGzT9PrUU4tfrG2xHVvN) (12 slides, exports to .pptx/PDF) · speaker notes in [docs/PITCH_NOTES.md](docs/PITCH_NOTES.md) |
| 3. Demo video (≤5 min, optional) | Shot list and narration: [docs/DEMO_VIDEO_SCRIPT.md](docs/DEMO_VIDEO_SCRIPT.md) |
| 4. Source code, README, deployment, tests | This repository · Quick start · Docker · `tests/` · CI |
| 5. Security self-assessment | [docs/SECURITY_SELF_ASSESSMENT.md](docs/SECURITY_SELF_ASSESSMENT.md) |
| 6. Execution evidence | [evidence/](evidence) (regenerate with `scripts/run_demo_scenario.py`) |

## Disclaimer

Prototype for a hackathon. Balances, merchants and bills are synthetic, and the bank is simulated. Steward gives general budgeting guidance, not personalised investment, tax or legal advice. Merchant and biller names appear only as realistic transaction descriptors. No affiliation is implied.
