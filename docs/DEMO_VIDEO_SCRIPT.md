# Demo video script (≤ 5 minutes)

**Setup before recording**
1. `pip install -r requirements-dev.txt`, set `ANTHROPIC_API_KEY` in `.env` (optional; the offline agent also works), then delete `data/steward.db` for a clean start.
2. `python -m uvicorn steward.asgi:app --port 8000` and open http://localhost:8000 at 1440×900, browser zoom 100%.
3. Use the sandbox demo password from `.env.example`.

| Time | Screen | Action | Narration (suggested) |
|---|---|---|---|
| 0:00–0:20 | Sign-up page | Show the hero text and the four tier cards | "Finance apps show you charts. Steward is an AI that actually takes part in your finances: it budgets, audits bills, pays and saves, but only as far as you allow." |
| 0:20–0:45 | Sign-up → Dashboard | Create the account with sandbox data | "We start with six months of sandbox banking for Alex, an analyst in Singapore. Balances, safe-to-spend until payday, a 30-day forecast with an uncertainty band, and a health score that explains itself." |
| 0:45–1:25 | Dashboard insights → Spending | Point at the duplicate Shopee charge, Netflix +15%, three video subscriptions, quarantined transaction | "Steward found a duplicate charge, a price hike, overlapping subscriptions, and a transaction whose description tries to instruct the AI. That one is quarantined: the model never sees it." |
| 1:25–2:05 | Bills | Open bills: SP Group spike, Singtel new VAS charge, StarHub clean. Bill analyzer → *Use a sample bill* → Analyze | "Every bill is audited line by line before anyone pays it. Usage up 38%, a premium-SMS charge that wasn't there last month. Paste any bill and it checks the arithmetic, the 9% GST, fees and new charges." |
| 2:05–2:35 | Budgets & goals | Show projected month-end, the 50/30/20 bars, then *Can I afford it?* with 1200 | "Budgets project the month-end from known bills plus your pace. Can Alex afford a $1,200 laptop? The answer comes from the forecast: the lowest balance after the purchase, the pessimistic path against the safety floor, and how many weeks it delays the emergency fund." |
| 2:35–3:30 | Agent | Chat: "What are my subscriptions?", then "Pay my StarHub bill" at Tier 1 (refused by tier). Select **Tier 3**, Save → password prompt. Ask "Pay my StarHub bill" again (executes), then "Pay my Singtel bill" (held). *Run daily check-up now* | "At Tier 1 the agent can organise but not pay. Raising it to Autopilot asks for my password again. Same request: the StarHub bill is clean, exact and allowlisted, so it's paid. Singtel has a new VAS charge, so it waits for me. The daily check-up also holds the SP Group bill because of the usage spike." |
| 3:30–3:55 | Agent → Waiting for you | Expand *Policy checks* on the SP Group card, Approve | "Every decision shows the checks: tier, ownership, balance floor, limits, the bill audit. I've reviewed the spike, so I approve." |
| 3:55–4:25 | Sandbox | +30 days | "Let's fast-forward a month on Autopilot: salary arrives, bills are issued, and Steward sweeps spare cash to savings each week and pays clean bills before the due date, always inside my limits." |
| 4:25–4:45 | Agent | Chat: "Send $500 to my friend" → refused. Click **Freeze agent** | "Some things are never delegated: paying people, adding payees, trading, changing its own limits. And one click freezes everything." |
| 4:45–5:00 | Audit trail | *Verify integrity* | "Every action is in a hash-chained audit log, and the ledger reconciles to the cent. Steward: an AI that participates in your finances, safely." |

**Recording tips:** use the Sandbox page's *Reset sandbox* between takes (the audit trail is kept). If showing Claude, run one or two chat questions before recording so the conversation history looks natural. Keep the cursor still while narrating numbers.
