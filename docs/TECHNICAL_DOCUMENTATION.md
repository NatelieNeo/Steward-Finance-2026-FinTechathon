# Steward: technical documentation

Version 1.0 · October 2026 · Theme: *AI as a Financial Participant* · Topic: Personal Finance Assistant

## 1. Goals

Steward is a personal-finance agent that does more than report. It **participates**: it budgets, audits bills, pays, saves, disputes and cancels on the user's behalf. The design goals were:

1. **Useful on day one.** Real insights from raw transactions (no manual tagging), and bills checked before they are paid.
2. **Safe delegation.** The user chooses how much the AI may do (Tiers 0–3). A deterministic policy engine, not the language model, decides what executes.
3. **Verifiable.** Every decision is explainable (per-check results) and recorded in a tamper-evident log. The ledger always reconciles.
4. **Deployable and scalable.** Stateless API, PostgreSQL, per-user data partitioning, no front-end build step, and the full suite runs offline in CI.

## 2. System architecture

```mermaid
flowchart LR
  subgraph Client
    UI["Web SPA<br/>(ES modules, SVG charts, strict CSP)"]
  end
  subgraph API["FastAPI app (stateless workers)"]
    MW["Middleware: security headers, global rate limit,<br/>request IDs, error envelope"]
    AUTH["auth routes<br/>sessions · CSRF · step-up"]
    FIN["finance routes<br/>overview · transactions · bills · budgets · goals"]
    AGT["agent routes<br/>policy · actions · chat · audit · sandbox"]
  end
  subgraph Services
    AN["Analysis<br/>(per-request cache of a user's data)"]
    SVC["AgentService<br/>propose → decide → execute"]
    CYC["Daily cycle planner"]
    CHAT["Chat orchestrator + Toolbox"]
  end
  subgraph Core["Pure engines (no I/O)"]
    ENG["categorizer · recurring · anomalies · forecast ·<br/>budgeting · health · goals · bill_audit · insights · importer"]
    POL["Policy engine<br/>(tiers, guardrails)"]
  end
  subgraph Infra
    LEDGER["Sandbox core-banking ledger"]
    DB[("PostgreSQL / SQLite")]
    AUD["Hash-chained audit log"]
    LLM["LLM provider<br/>Claude · offline · scripted"]
  end
  UI -- "JSON over HTTPS (cookie + CSRF header)" --> MW --> AUTH & FIN & AGT
  FIN --> AN --> ENG
  AGT --> SVC --> POL
  AGT --> CYC --> SVC
  AGT --> CHAT --> LLM
  CHAT -- "tool calls" --> SVC
  SVC --> LEDGER --> DB
  SVC --> AUD --> DB
  AN --> DB
```

**Key separation:** the LLM can only *call tools*. Read tools return minimised, sanitised data. The single write tool, `propose_action`, hands a request to `AgentService`, which validates it against a typed schema and evaluates it with the **pure** policy engine. Only then does it execute on the ledger, queue for approval, or deny. The same path serves the daily cycle, chat, and one-click insight actions.

### 2.1 Lifecycle of an agent action

```mermaid
sequenceDiagram
  participant U as User
  participant L as LLM (Claude)
  participant T as Toolbox
  participant S as AgentService
  participant P as Policy engine
  participant B as Ledger
  participant A as Audit log
  U->>L: "Pay my StarHub bill"
  L->>T: get_bills()
  T-->>L: open bills (sanitised) + audit verdicts
  L->>T: propose_action(pay_bill, {bill_id})
  T->>S: propose(type, params, origin=chat)
  S->>S: idempotency key lookup · pydantic validation · normalise defaults
  S->>P: evaluate(type, params, context)
  P-->>S: ALLOW / REQUIRE_APPROVAL / DENY + per-check results
  S->>A: action.proposed (decision, reasons)
  alt ALLOW
    S->>P: re-evaluate at execution time
    S->>B: post debit, mark bill paid
    S->>A: action.executed
  else REQUIRE_APPROVAL
    S-->>U: queued (expires in 48h, step-up if amount ≥ threshold)
  else DENY
    S-->>U: refused with reasons
  end
  T-->>L: {status, reasons}
  L-->>U: reply reporting the real status
```

### 2.2 Action state machine

```mermaid
stateDiagram-v2
  [*] --> denied: DENY
  [*] --> proposed: ALLOW
  [*] --> pending_approval: REQUIRE_APPROVAL
  proposed --> executed
  proposed --> failed: re-check fails / ledger error
  pending_approval --> executing: approve (conditional UPDATE)
  pending_approval --> rejected: reject or kill switch
  pending_approval --> expired: after 48h (re-raised by the next cycle)
  executing --> executed
  executing --> failed: re-check fails (e.g. balance dropped)
```

Leaving `pending_approval` is a conditional `UPDATE … WHERE status='pending_approval'`, so a double-click or two devices can never execute an approval twice (tested).

## 3. Components

| Module | Responsibility |
|---|---|
| `steward/core/money.py` | Integer-cents arithmetic (Decimal at the edges, half-up rounding), month maths, formatting |
| `core/merchant.py` | Raw descriptor → canonical merchant (prefix stripping, token filtering, alias table) |
| `core/categorizer.py` | Five-stage categorisation cascade, including a Naive Bayes model |
| `core/recurring.py` | Subscription, bill and income detection; price-change detection; projection |
| `core/anomalies.py` | Duplicate charges, fees, outliers, bill spikes, price hikes, overlapping subscriptions |
| `core/forecast.py` | Probabilistic cash-flow forecast and affordability |
| `core/budgeting.py` | Budget suggestions, 50/30/20 analysis, month-end projections |
| `core/health.py` | Six-component financial health score |
| `core/goals.py` | Goal progress and safe-to-save |
| `core/bill_audit.py` | Bill text parser and line-item audit |
| `core/importer.py` | CSV statement import (auto-detected columns, dates, amounts; dedupe fingerprints) |
| `core/insights.py` | Converts findings into ranked insights with suggested actions |
| `agent/policy.py` | Tiers, risk classes, guardrails → `ALLOW` / `REQUIRE_APPROVAL` / `DENY` |
| `agent/actions.py` | Strict pydantic schemas for every action's parameters |
| `agent/service.py` | Action lifecycle, idempotency, approval, execution, execution-time re-check |
| `agent/cycle.py` | Daily planner (alerts, budgets, bills, disputes, cancellations, sweeps) |
| `agent/tools.py` | 11 LLM tools (10 read, 1 propose) with data minimisation |
| `agent/llm.py` | Claude provider (manual tool loop), offline intent router, scripted (attack) provider |
| `agent/digest.py` | Weekly digest with numeric grounding check |
| `security/*` | scrypt passwords, sessions/CSRF/step-up, rate limiter, untrusted-text handling, audit chain |
| `sandbox/*` | Ledger operations and invariant check, synthetic persona, day-by-day simulator |
| `services/analysis.py` | Loads a user's data once per request and runs the engines lazily |
| `api/*`, `main.py` | 50 REST endpoints under `/api/v1`, middleware, error envelope, static UI |

## 4. Data model

```mermaid
erDiagram
  USER ||--o{ ACCOUNT : owns
  USER ||--o{ TRANSACTION : owns
  ACCOUNT ||--o{ TRANSACTION : posts
  USER ||--o{ PAYEE : has
  PAYEE ||--o{ BILL : issues
  USER ||--o{ BUDGET : sets
  USER ||--o{ GOAL : saves
  USER ||--|| AGENT_POLICY : grants
  USER ||--o{ AGENT_ACTION : "agent proposes"
  USER ||--o{ AUDIT_ENTRY : "hash chain"
  USER ||--o{ AUTH_SESSION : signs-in
  USER ||--o{ ALERT : receives
  USER ||--o{ MERCHANT_MEMORY : teaches
  TRANSACTION ||--o| DISPUTE : "may be disputed"
```

- **Money** is `BIGINT` cents everywhere; negative = outflow. Floats never touch balances.
- **Transactions** carry `merchant`, `category`, `category_source` (rule, memory, lexicon, model, llm, user, quarantine), a confidence, `kind` (purchase, income, transfer, bill_payment, fee, refund), `flags` (e.g. `prompt_injection_suspected`) and a per-user unique `import_hash`.
- **AgentAction** stores the validated params, risk class, decision, human-readable reasons, the full per-check list, step-up flag, origin (cycle, chat, insight), a per-user unique **idempotency key**, the result, and sandbox dates.
- **AuditEntry** has `(user_id, seq)` unique, with `prev_hash` and `hash`.
- Every query is scoped by `user_id`, so the schema partitions cleanly by user.

## 5. Core algorithms

### 5.1 Merchant normalisation
Upper-case the descriptor and strip punctuation. Repeatedly remove rail prefixes (`POS`, `NETS`, `VISA`, `PAYNOW`, `GIRO`, `BILL PAYMENT`, …). Match against an alias table of about 60 canonical merchants (`GRAB*RIDE` → Grab, `GOOGLE *YOUTUBE` → YouTube Premium). Otherwise drop tokens containing digits (store IDs, references, dates), location and legal suffixes (`SINGAPORE`, `PTE`, `LTD`), then keep the first four tokens in title case.

### 5.2 Categorisation cascade
First confident answer wins:

1. **User rules** (substring, never regex: no ReDoS) → 2. **Merchant memory** learned from the user's corrections (a correction also re-labels that merchant's other auto-categorised transactions) → 3. **Lexicon** of canonical merchants plus keyword rules (fees, salary, rent…) → 4. **Multinomial Naive Bayes** over word unigrams and bigrams trained on a labelled corpus, with Laplace smoothing:

$$P(c\mid d) \propto P(c)\prod_{w\in d}\frac{n_{c,w}+\alpha}{N_c+\alpha|V|}$$

The prediction is accepted when the posterior is at least 0.55 (computed in log space and normalised) → 5. "Other" (or Income for unexplained credits). An LLM batch stage for low-confidence items fits between 4 and 5 but is not implemented in this prototype. A positive amount from a shop category becomes Income (refund). Quarantined descriptors skip the cascade.

### 5.3 Recurring payments (subscriptions, bills, income)
For each (merchant, direction) group, with same-day duplicates collapsed:

- **Cadence:** match the median inter-payment gap to {weekly 7±1.5, biweekly 14±2.5, monthly 30.44±4, bimonthly, quarterly 91.3±10, semiannual, annual 365.25±20} days.
- **Regularity:** the share of gaps within tolerance. A late payment followed by an early one (two gaps summing to 2× the period ± 2·tol) counts as regular, so one slip does not hide a real bill. Require ≥ 0.6, and ≥ 3 occurrences (≥ 2 for periods over 60 days).
- **Amount stability:** coefficient of variation. Subscriptions must be flat (CV ≤ 2%, or flat apart from one clean price step), bills may vary (CV ≤ 0.6), income CV ≤ 0.3.
- **Price change:** the trailing run of identical amounts is the current price. If every earlier amount was also identical (± 2 cents), the step is a price change: $\Delta = (p_{new}-p_{old})/p_{old}$, with |Δ| ≥ 2% and a change date. Variable bills never produce false "hikes".
- **Projection:** monthly cadences keep the modal day-of-month (clamped to month end), and day cadences add the period. Monthly equivalent = amount × 30.44 / period. **Active** while today ≤ next date + max(2·tol, period/2).
- **Confidence** = 0.5·regularity + 0.3·min(1, n/6) + 0.2·(1 − min(1, CV)).
- **Kind:** income (inflow), subscription (fixed and subscription-like), bill (housing, utilities, insurance, debt, education), or other.

### 5.4 Anomaly detection
- **Duplicate charge:** same merchant and amount within 2 days, amount at least S$10, excluding habitual merchants (6 or more visits a month).
- **Fees:** a regex over descriptors (late, FX, overdraft, annual, ATM, admin, penalty…) plus fee lines inside bill statements.
- **Large purchase:** within a category (at least 8 samples), modified z-score $z = 0.6745\,(x-\tilde{x})/\mathrm{MAD}$ ≥ 3.5 **and** x ≥ 3 × the category's 90th percentile. The second guard stops bimodal categories (hawker vs restaurant) from flagging every dinner.
- **Bill spike:** latest variable-bill payment ≥ 25% above the median of the previous six.
- **Overlap:** two or more active subscriptions in the same group (video, music, cloud storage). The saving reported is at least the cheapest one's annual cost.

### 5.5 Probabilistic cash-flow forecast
For the checking account, day by day over horizon H:

$$\mu_t = \mu_{t-1} + \sum \text{events}_t - m_{\text{dow}(t)}, \qquad \sigma_t^2 = \sigma_{t-1}^2 + s^2_{\text{dow}(t)}$$

- **Events:** projected occurrences of active recurring series (salary, rent, subscriptions) plus open bill statements on their due dates. A series occurrence within 20 days of an open statement for the same merchant is skipped, so bills are not double-counted.
- **Discretionary profile** $(m_{dow}, s^2_{dow})$: recency-weighted (decay 0.9 per week) mean and variance of daily non-recurring spend over the last 12 **complete** weeks. One-off purchases above max(S$300, 8 × median transaction) are excluded from the baseline.
- **Band:** P10/P90 = $\mu_t \mp 1.2816\,\sigma_t$. Overdraft probability $= \max_t \Phi(-\mu_t/\sigma_t)$.
- **Safe to spend** = min P10 balance until the next payday − the user's floor.
- **Affordability** of a purchase A on date d: subtract A from every point from d onward (45-day horizon). Verdict: *not affordable* if min μ < 0, *risky* if min P10 < floor, otherwise *affordable*. It also reports the share of a typical month's discretionary spend and the delay to the top goal.

### 5.6 Budgeting
- **Suggestions:** per category, the median of the last three full months, rounded up to S$10. If wants exceed 30% of income (50/30/20), want-category suggestions are trimmed proportionally, never below 70% of history. Fee categories are suggested at zero.
- **Projection:** month-end = spent + recurring charges and open bills still due this month + remaining days × a flexible daily rate. The rate blends history and this month by month progress, $r = f\cdot r_{obs} + (1-f)\cdot r_{hist}$, and is computed on **non-recurring** spend only, so rent paid on the 1st is never extrapolated. With no history, the observed pace is used. Status is *over*, *at risk* (projection above the limit) or *on track*.

### 5.7 Financial health score (0–100)
A weighted sum of piecewise-linear component scores:

| Component | Weight | Mapping |
|---|---|---|
| Savings rate (3-month) | 25% | −10% → 0, 0% → 30, 10% → 65, 20% → 90, 30% → 100 |
| Emergency fund (months of essentials) | 20% | 0 → 0, 1 → 30, 3 → 70, 6 → 100 |
| Avoidable fees (count, 90 days) | 15% | 100 − 25 per fee |
| Budget adherence | 15% | share of budgets on track |
| Spending stability (CV of wants) | 10% | 0 → 100, 0.3 → 60, 1.0 → 0 |
| Debt load (revolving ÷ income) | 15% | 0 → 100, 0.3 → 75, 1 → 20, 2 → 0 |

Grade A ≥ 85, B ≥ 70, C ≥ 55, D ≥ 40, else E. The two weakest components become the "focus" tips.

### 5.8 Safe-to-save (autonomous sweep sizing)
$$\text{sweep} = \left\lfloor \tfrac{1}{2}\,(\min_t P10_t - \text{floor}) \right\rfloor_{S\$10}$$

This is capped by the per-action limit and the goal's remaining amount, and runs at most once per ISO week (scope-only idempotency key). It uses only half of the *pessimistic* headroom.

### 5.9 Bill audit
Line items come from statements or pasted text, using a regex for trailing amounts with parentheses, CR/DR, S$ and thousands separators. Lines are classified as charge, fee, tax or discount. Checks:

1. **Arithmetic:** the items must sum to the stated total.
2. **GST:** the tax line must be within 5 cents of 9% of the taxable amount (Singapore GST since 2024).
3. **Fees:** late, admin, reconnection and penalty lines are flagged.
4. **New lines:** items absent from the previous bill. Risky ones (VAS, premium SMS, roaming, rentals, add-ons) are warnings.
5. **Line increase:** over 20% versus the previous bill.
6. **Spike:** total over 25% above the median of earlier bills.

The verdict is *dispute* (critical), *review* (warning), *ok with notes* or *ok*. The potential saving sums fees, arithmetic and tax discrepancies, and risky new charges. A bill with a verdict of *review* or *dispute* is never auto-paid.

### 5.10 Insights and the grounded digest
Findings, budget pace, low-balance risk, the savings opportunity, upcoming bills, month-over-month change, quarantined text and the month summary become insights. Each has severity, an amount and an optional one-click action. They are ranked by severity, then by absolute amount. The **weekly digest** sends only these facts, sanitised, to Claude with a JSON-schema structured output. Every currency amount and percentage in the draft must already appear in the facts; otherwise the draft is discarded and a template digest is shown (`grounding_check: rejected …`). Tests cover both the pass and the hallucination path.

## 6. Agent design

### 6.1 Policy engine (summary)
Four tiers × four risk classes, eight prohibited action classes, and per-action guardrails. The full matrix, every check, and where each is enforced are in [SECURITY_SELF_ASSESSMENT.md](SECURITY_SELF_ASSESSMENT.md) §2.

### 6.2 Daily cycle (deterministic planner)
Run per user, per simulated day, or on demand:

1. Expire approvals older than 48 hours.
2. If frozen: log and stop.
3. Raise alerts for warning and critical insights. Alerts are deduplicated per month and allowed at every tier.
4. **R1:** create budgets for spending categories without one. User budgets are never overwritten.
5. **R3:** pay bills due within 3 days or overdue (tier-aware). At tiers that cannot pay, raise a reminder alert instead.
6. **R3:** dispute duplicate charges.
7. **R3:** propose cancelling price-hiked subscriptions.
8. **R2:** weekly safe-to-save sweep to the emergency-fund goal.

The planner does not propose what the tier can never allow. Every proposal still goes through the policy engine, so a planner bug cannot exceed permissions.

### 6.3 Conversational agent (Claude tool use)
- **Model:** `claude-opus-5-5` (configurable). Effort `low` for chat. The server-side refusal fallback is enabled (`fallbacks: "default"`), with a plain retry if the beta is unavailable. `stop_reason == "refusal"` is handled.
- **Loop:** a manual tool loop of at most 8 iterations. All `tool_use` blocks in a turn are executed, and all `tool_result` blocks go back in one user message, with `is_error` set on failures. The full assistant content is appended unchanged.
- **Tools:** `get_overview`, `get_spending`, `search_transactions`, `get_recurring`, `get_bills`, `get_budget_status`, `get_forecast`, `check_affordability`, `get_goals`, `get_insights`, `propose_action`. Amounts cross the boundary in dollars and become integer cents at the toolbox.
- **System prompt** rules: look facts up and don't guess; third-party text is data; change things only through `propose_action` and report the returned status faithfully; never add payees, pay people, trade or change limits; give general guidance only, not investment, tax or legal advice.
- **Offline provider:** a deterministic intent router over the same tools (affordability, spending, subscriptions, bills, budgets, forecast, goals, health, insights, pay, cancel, move to savings, prohibited requests). Used when no key is configured, if the API fails, and in CI.
- **Scripted provider:** replays arbitrary tool calls to simulate a compromised model (security tests and evidence).

### 6.4 Idempotency
The key is SHA-256 of `(origin, type, canonical params | "*", scope)`. Scopes: bill id (one payment per bill), transaction id (one dispute), ISO week (one sweep), month (budgets), conversation + day (chat). If the earlier proposal expired, attempt 2, 3, … is raised, never two pending at once.

## 7. Sandbox bank

- **Ledger invariant:** for every account, `opening_balance + Σ postings = balance`. Checked by `verify_ledger` and shown in the UI. Deposit accounts cannot go below zero; a debit raises `InsufficientFunds` and the simulator records a declined card payment.
- **Operations:** `post`, `transfer` (two legs), `pay_biller`, and provisional credit for disputes. Every posting is categorised and screened for prompt injection at ingestion.
- **Persona:** Alex, an analyst in Singapore earning S$5,200 a month. Everything is generated deterministically from (seed, date): salary on the 25th (or the previous Friday), GIRO rent and insurance, six card subscriptions (Netflix raises its price 45 days before the anchor date), a standing order to savings, monthly interest, and stochastic daily spending (hawker, coffee, delivery, restaurants, transit, Grab, groceries, shopping, cinema, pharmacy, FX purchases with fees). Three billers issue statements with line items, GST and occasional roaming or VAS charges. Planted history covers a duplicate charge, a S$1,849 purchase, a late payment, a usage spike and an injection-laden descriptor.
- **Simulation day:** post activity → issue statements (with a late charge if the previous bill was paid late) → mark overdue bills with alerts → run the agent's daily cycle under the current tier → write to the audit log.

## 8. API

There are 50 JSON endpoints under `/api/v1`. OpenAPI docs are at `/api/docs` outside production. Errors use a uniform envelope, `{"error": {"code", "message"}}`.

| Group | Endpoints |
|---|---|
| Auth | `POST /auth/register`, `/auth/login`, `/auth/logout`, `/auth/step-up`, `GET /auth/me` |
| Overview & analysis | `GET /overview`, `/spending`, `/insights`, `/digest`, `/recurring`, `/forecast`, `/health`; `POST /affordability` |
| Transactions | `GET /transactions`, `PATCH /transactions/{id}`, `POST /transactions/import`, `GET /transactions/export.csv` |
| Bills | `GET /bills`, `GET /bills/{id}`, `POST /bills/{id}/pay`, `POST /bills/analyze` |
| Budgets & goals | `GET /budgets`, `PUT/DELETE /budgets/{category}`, `GET/POST /goals`, `DELETE /goals/{id}` |
| Payees & alerts | `GET /payees`, `PATCH /payees/{id}`, `GET /alerts`, `POST /alerts/{id}/read` |
| Agent | `GET/PUT /agent/policy`, `POST /agent/freeze`, `/agent/unfreeze`, `/agent/cycle`, `GET/POST /agent/actions`, `POST /agent/actions/{id}/approve`, `/reject`, `POST /agent/chat`, `GET /agent/chat/{id}` |
| Audit & sandbox | `GET /audit`, `/audit/verify`, `/audit/export.jsonl`, `GET /sandbox`, `POST /sandbox/advance`, `/sandbox/reset` |

## 9. Scalability and deployment

- **Stateless API workers.** Sessions live in the database (hashed tokens), so any worker can serve any request. Scale horizontally behind a load balancer (`uvicorn --workers N` per container, N containers).
- **Database.** PostgreSQL in production (`DATABASE_URL=postgresql+psycopg://…`), with a connection pool (size 10, overflow 20, pre-ping). Every table is keyed by `user_id`, so sharding or partitioning by user is straightforward. Hot paths are indexed (`transactions(user_id, posted_on)`, merchant, category, action status).
- **Concurrency.** Approval claims are conditional updates. Audit appends are serialised per user with a PostgreSQL transaction-scoped advisory lock, plus the `UNIQUE(user_id, seq)` backstop.
- **Background work.** The daily cycle is a pure function of one user's data, so production runs it as a queue job per user (e.g. a scheduler that enqueues users whose local date changed, consumed by workers). It parallelises across users.
- **Compute.** `Analysis` loads about a year of a user's transactions once per request and computes engines lazily. Typical requests take 10–80 ms on the 600-transaction persona. Per-user results can be cached and invalidated on new postings when needed.
- **Rate limiting** is in-memory per process. Swap in Redis (same three-method interface) for multi-instance deployments.
- **Deploy:** `docker compose up --build` gives the API plus PostgreSQL. For production: set `STEWARD_ENV=production` (disables API docs), `COOKIE_SECURE=true` (also sends HSTS), terminate TLS at the proxy, run uvicorn with `--proxy-headers`, store `ANTHROPIC_API_KEY` in a secret manager, and add Alembic migrations before the first schema change (tables are created on start-up in this prototype).
- **Observability.** Every response carries `X-Request-ID`. Logs record method, path, status and latency only, never bodies, cookies or query strings. `/healthz` is used for liveness checks.

## 10. Testing strategy

| Layer | What is tested | File |
|---|---|---|
| Pure engines | money, merchants, categorizer, recurring, anomalies, forecast, budgets, health, goals, bill audit, importer | `test_core_basics.py`, `test_core_analytics.py`, `test_bills_and_import.py` |
| Policy | full 4 × 7 tier matrix, 8 prohibited × 4 tiers, kill switch, limits, floor, ceiling, velocity, bills, disputes, approval re-check | `test_policy.py` |
| Security primitives | scrypt, injection detection (positive and negative), PII redaction, rate limiter, audit tamper detection | `test_security_primitives.py` |
| HTTP security | headers, cookies, enumeration, brute force, CSRF, idle expiry, step-up, limit consistency, tenant isolation | `test_api_security.py` |
| End-to-end | tiers in the cycle, autopay, sweeps, exactly-once approval, step-up, TOCTOU, kill switch, disputes, cancellations, late fees, 45-day simulation, chat, PII, compromised model, CSV import, learning | `test_agent_flows.py` |
| Grounding | digest accepts grounded and rejects hallucinated numbers | `test_digest.py` |

Total: **222 tests**, all offline. `scripts/run_demo_scenario.py` adds an end-to-end evidence run, which CI uploads as an artifact.

## 11. Limitations and roadmap

- **Data connectivity:** sandbox and CSV only. The next step is consented account aggregation (e.g. SGFinDex-style consent flows), bank-statement PDF parsing, and e-bill presentment.
- **Payments:** simulated. Real rails (PayNow/FAST, GIRO mandates) would sit behind the same policy engine, with the bank's own limits and confirmation as an extra layer.
- **Models:** the categorizer is a small Naive Bayes model, which is explainable and fast. Next steps are per-user retraining from corrections, and an LLM batch stage (structured outputs, PII-redacted) for low-confidence items.
- **Migrations, multi-currency and household accounts** are not yet implemented.
