# Pitch: speaker notes (10 minutes)

**Slide deck:** https://claude.ai/artifact/VKjGzT9PrUU4tfrG2xHVvN (12 slides; downloadable as .pptx or PDF from the deck's menu; private until shared from its Share menu). Full speaker notes are on each slide. Fill in the `[Team name]`, `[Member names]`, repository and video placeholders on slides 1 and 12.

| # | Slide | Time | Key message |
|---|---|---|---|
| 1 | Steward (cover) | 0:20 | An AI that participates in your finances, only as far as you allow. The four bars are the tiers. |
| 2 | Money leaks quietly | 0:45 | Six real findings from the sandbox customer: duplicate, price hike, overlap, bill spike, VAS add-on, hidden late fee. |
| 3 | Steward does the work | 0:35 | Budgeting, bill analysis, insights, and it acts. |
| 4 | One loop, every day | 3:15 | Observe → Audit → Decide → Act & record, then the **live demo** (see DEMO_VIDEO_SCRIPT.md for the click path). |
| 5 | You choose how far it goes | 0:45 | Tier staircase; never-delegated actions; password to go up. |
| 6 | The model proposes. Code decides. | 0:45 | Schema → policy engine → allow / ask / deny; re-check at execution; audit chain. |
| 7 | Statement | 0:25 | "The language model is never trusted. It can only ask." The compromised-model result. |
| 8 | Engines under the hood | 0:45 | Categorizer, recurring and price hikes, probabilistic forecast, bill audit, budgets, grounded digest. |
| 9 | Proven on the sandbox | 0:45 | 222 tests, 30-day Autopilot, S$2,112 moved inside limits, tamper caught at #9. |
| 10 | Security, honestly assessed | 0:35 | In place versus known risks (22 tracked). |
| 11 | Built to grow | 0:25 | Stateless API, per-user partitioning, Docker, CI, offline mode. |
| 12 | What's next · close | 0:20 | Real accounts and rails behind the same policy engine; passkeys; smarter models. |

**Likely judge questions**
- *What stops a prompt-injected model from draining the account?* The model has no authority. It calls `propose_action`, which goes through schema validation, then the policy engine (tier, ownership, floor, limits, hard ceiling), then approval with step-up, then a re-check at execution. In our evidence run the injected model claimed "Done!" and every money action was denied or held.
- *Why not let the LLM decide?* Money rules must be deterministic, testable and explainable. We test the full tier × action matrix (28 cases) and every prohibited action at every tier (32 cases).
- *Isn't Autopilot risky?* Only routine, low-variance actions run unattended: sweeps sized at half of the pessimistic forecast headroom, and statement-exact payments of clean bills to billers you allowlisted. Anything unusual (a usage spike, a new VAS charge, an amount over your limit) comes back to you.
- *What about privacy?* The model sees merchants, amounts and dates; PII is redacted; quarantined text never leaves the server; offline mode needs no third party.
- *How does it scale?* Each user's data and daily cycle is independent, so it shards by user and runs the cycle as queue jobs.
- *Where do the numbers on slide 2 come from?* The product's own insights on the synthetic sandbox persona (regenerate with `scripts/run_demo_scenario.py`; see evidence/EXECUTION_EVIDENCE.md, phase P1).
