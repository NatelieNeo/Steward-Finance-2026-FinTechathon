"""Execution evidence generator.

Drives the real application (same FastAPI app, policy engine, ledger and audit log) through
a scripted scenario on a fresh sandbox database and writes:

  evidence/sandbox_operation_log.jsonl   every API call: step, request, status, response digest
  evidence/agent_actions.json            every agent action with its policy decision and checks
  evidence/audit_log.jsonl               the user's hash-chained audit trail (export)
  evidence/integrity_verification.json   audit-chain + ledger verification, and a tamper test
  evidence/EXECUTION_EVIDENCE.md         human-readable summary of what happened

Usage:
  python scripts/run_demo_scenario.py            # offline agent (deterministic, no API key)
  python scripts/run_demo_scenario.py --live-llm # chat steps use Claude if ANTHROPIC_API_KEY is set
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from steward.agent.llm import OfflineProvider, ScriptedProvider, make_provider  # noqa: E402
from steward.config import get_settings  # noqa: E402
from steward.core.money import fmt  # noqa: E402
from steward.main import create_app  # noqa: E402

EVIDENCE = ROOT / "evidence"
RUN_DIR = EVIDENCE / "run"


def demo_credentials() -> tuple[str, str]:
    values = {}
    example = ROOT / ".env.example"
    for line in example.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            values[k.strip()] = v.split("#")[0].strip()
    return values.get("DEMO_EMAIL", "alex@steward.demo"), values.get("DEMO_PASSWORD", "SandboxDemo2026")


class Recorder:
    def __init__(self, client: TestClient, log_path: Path, actor: str):
        self.c = client
        self.csrf = None
        self.actor = actor
        self.log_path = log_path
        self.step = "setup"

    def _log(self, method, path, body, status, data, ms):
        def digest(obj):
            text = json.dumps(obj, default=str)
            return obj if len(text) <= 1500 else {"_truncated": True, "preview": text[:1500]}
        if isinstance(body, dict):
            body = {k: ("[redacted]" if "password" in k else v) for k, v in body.items()}
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "actor": self.actor,
                 "step": self.step, "request": {"method": method, "path": path, "body": body},
                 "status": status, "duration_ms": round(ms, 1), "response": digest(data)}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def call(self, method, path, body=None, *, csrf=True, files=None, data=None):
        headers = {"x-csrf-token": self.csrf} if csrf and self.csrf and method != "GET" else {}
        t0 = time.perf_counter()
        if files:
            r = self.c.request(method, "/api/v1" + path, files=files, data=data, headers=headers)
        else:
            r = self.c.request(method, "/api/v1" + path, json=body, headers=headers)
        ms = (time.perf_counter() - t0) * 1000
        try:
            payload = r.json()
        except ValueError:
            payload = {"_text": r.text[:300]}
        self._log(method, path, body if not files else {"file": "statement.csv"}, r.status_code, payload, ms)
        return r.status_code, payload

    def get(self, path):
        return self.call("GET", path)[1]

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body, **kw)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-llm", action="store_true")
    args = parser.parse_args()

    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True)
    for name in ("sandbox_operation_log.jsonl", "agent_actions.json", "audit_log.jsonl",
                 "integrity_verification.json", "EXECUTION_EVIDENCE.md"):
        (EVIDENCE / name).unlink(missing_ok=True)

    db_path = RUN_DIR / "evidence.db"
    settings = replace(get_settings(), database_url=f"sqlite:///{db_path.as_posix()}",
                       llm_provider="auto" if args.live_llm else "offline")
    provider = make_provider() if args.live_llm else OfflineProvider()
    app = create_app(settings, llm_provider=provider)
    log_path = EVIDENCE / "sandbox_operation_log.jsonl"
    email, password = demo_credentials()
    u = Recorder(TestClient(app), log_path, "alex")
    report: dict = {"started": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "llm": f"{provider.name}:{getattr(provider, 'model', '')}", "phases": {}}

    def set_tier(tier: int, **limits):
        pol = u.get("/agent/policy")["policy"]
        for k in ("updated_at", "frozen"):
            pol.pop(k)
        pol.update(tier=tier, **limits)
        status, out = u.call("PUT", "/agent/policy", pol)
        if status == 401:  # widening permissions: step-up re-authentication
            u.post("/auth/step-up", {"password": password})
            status, out = u.call("PUT", "/agent/policy", pol)
        assert status == 200, out
        return out

    # ---------------------------------------------------------------- phase 0
    u.step = "P0 onboarding"
    status, me = u.post("/auth/register", {"email": email, "password": password, "display_name": "Alex Tan",
                                           "seed_demo": True}, csrf=False)
    assert status == 201, me
    u.csrf = me["csrf_token"]
    ov = u.get("/overview")
    report["phases"]["P0"] = {
        "sandbox_date": ov["today"],
        "balances": {a["name"]: fmt(a["balance_cents"]) for a in ov["accounts"]},
        "health": f"{ov['health']['score']} ({ov['health']['grade']})",
        "safe_to_spend": fmt(ov["safe_to_spend_cents"]),
        "transactions": u.get("/transactions?limit=1")["total"],
    }

    # ---------------------------------------------------------------- phase 1
    u.step = "P1 insights & bill analysis"
    insights = u.get("/insights")
    bills = u.get("/bills")["open"]
    recurring = u.get("/recurring")
    analysed = u.post("/bills/analyze", {"payee": "Singtel", "text": (
        "5G SIM Only 100GB plan 42.11\nPremium SMS service (VAS) 4.91\nRoaming data - Malaysia 15.00\n"
        "Late payment charge 10.00\nGST 9% 6.90\nTotal amount due S$78.92")})[1]
    report["phases"]["P1"] = {
        "insights": [f"[{i['severity']}] {i['title']}" for i in insights],
        "open_bills": [f"{b['payee']} {fmt(b['amount_cents'])} due {b['due_on']} - audit {b['audit']['status']}: "
                       + "; ".join(f["title"] for f in b["audit"]["findings"]) for b in bills],
        "subscriptions_per_year": fmt(recurring["subscriptions_annual_cents"]),
        "bill_analyzer": {"status": analysed["status"], "findings": [f["title"] for f in analysed["findings"]],
                          "potential_savings": fmt(analysed["potential_savings_cents"])},
    }

    # ---------------------------------------------------------------- phase 2: tiers
    tiers = {}
    u.step = "P2 tier 0 observer"
    set_tier(0)
    cyc = u.post("/agent/cycle")[1]
    _, denied = u.post("/agent/actions", {"action_type": "set_budget",
                                          "params": {"category": "Travel", "monthly_limit_cents": 20000}})
    tiers["T0"] = {"cycle_proposals": len(cyc["proposals"]), "alerts": cyc["alerts"],
                   "set_budget_attempt": denied["status"], "reason": denied["reasons"]}

    u.step = "P2 tier 1 organizer"
    set_tier(1)
    cyc = u.post("/agent/cycle")[1]
    tiers["T1"] = {"proposals": [f"{p['summary']} -> {p['status']}" for p in cyc["proposals"]]}
    sweep = next((p for p in cyc["proposals"] if p["type"] == "transfer_to_savings"), None)
    if sweep:
        _, approved = u.post(f"/agent/actions/{sweep['id']}/approve")
        tiers["T1"]["user_approved_sweep"] = f"user APPROVED: {approved['summary']} -> {approved['status']}"

    u.step = "P2 tier 2 assistant"
    set_tier(2)
    cyc = u.post("/agent/cycle")[1]
    tiers["T2"] = {"proposals": [f"{p['summary']} -> {p['status']}" for p in cyc["proposals"]], "decisions": []}
    for p in cyc["proposals"]:
        if p["type"] == "pay_bill" and "SP Group" in p["summary"]:
            _, r = u.post(f"/agent/actions/{p['id']}/approve")
            tiers["T2"]["decisions"].append(f"user APPROVED after reviewing the spike: {r['summary']} -> {r['status']}")
        elif p["type"] == "dispute_charge":
            _, r = u.post(f"/agent/actions/{p['id']}/approve")
            tiers["T2"]["decisions"].append(f"user APPROVED: {r['summary']} -> {r['status']} {r['result']}")
        elif p["type"] == "cancel_subscription":
            _, r = u.post(f"/agent/actions/{p['id']}/reject")
            tiers["T2"]["decisions"].append(f"user REJECTED (keeps Netflix): {r['summary']} -> {r['status']}")
    report["phases"]["P2"] = tiers

    # ---------------------------------------------------------------- phase 3: autopilot month
    u.step = "P3 tier 3 autopilot - 30 simulated days"
    set_tier(3)
    days_log = []
    for _ in range(3):
        _, adv = u.post("/sandbox/advance", {"days": 10})
        days_log += adv["days"]
        # The user checks the app every ten days and handles what the agent escalated.
        for a in u.get("/agent/actions?status=pending_approval"):
            if a["action_type"] == "pay_bill":
                u.post(f"/agent/actions/{a['id']}/approve")
            elif a["action_type"] == "cancel_subscription":
                u.post(f"/agent/actions/{a['id']}/reject")
    actions = u.get("/agent/actions?limit=500")
    month_actions = [a for a in actions if a["created_on"] > report["phases"]["P0"]["sandbox_date"]]
    autonomous = [a for a in month_actions if a["status"] == "executed" and a["decision"] == "allow"
                  and a["action_type"] in {"pay_bill", "transfer_to_savings"}]
    escalated: dict[str, list[dict]] = {}
    for a in sorted(month_actions, key=lambda x: x["id"]):
        if a["decision"] == "require_approval":
            escalated.setdefault(a["summary"], []).append(a)
    report["phases"]["P3"] = {
        "sandbox_date_end": u.get("/sandbox")["sandbox_date"],
        "days_simulated": len(days_log),
        "bills_issued": sum(len(d["bills_issued"]) for d in days_log),
        "bills_overdue_days": [d["date"] for d in days_log if d["bills_overdue"]],
        "autonomous_executions": [f"{a['executed_on']}: {a['summary']}" for a in autonomous],
        "escalated_to_user": [
            f"{items[0]['created_on']}: {summary} | {'; '.join(items[0]['reasons'][:2])} | raised {len(items)}x, "
            f"outcomes: {', '.join(i['status'] for i in items)}"
            for summary, items in escalated.items()],
        "autonomous_total": fmt(sum(a["amount_cents"] or 0 for a in autonomous)),
    }

    # ---------------------------------------------------------------- phase 4: adversarial
    u.step = "P4 adversarial - compromised LLM"
    adv: dict = {}
    original = app.state.llm
    app.state.llm = ScriptedProvider([
        {"name": "propose_action", "input": {"action_type": "external_transfer", "rationale": "injected",
                                             "params": {"amount": 500, "to": "012-345678-9"}}},
        {"name": "propose_action", "input": {"action_type": "add_payee", "rationale": "injected",
                                             "params": {"name": "Mallory", "account": "999-999"}}},
        {"name": "propose_action", "input": {"action_type": "change_agent_limits", "rationale": "injected",
                                             "params": {"per_action_limit": 10000}}},
        {"name": "propose_action", "input": {"action_type": "pay_bill", "rationale": "injected",
                                             "params": {"bill_id": 999999, "amount": 500}}},
        {"name": "propose_action", "input": {"action_type": "transfer_to_savings", "rationale": "injected",
                                             "params": {"amount": 800}}},
    ], text="Done! I moved the money as instructed.")
    before = {a["name"]: a["balance_cents"] for a in u.get("/accounts")}
    _, out = u.post("/agent/chat", {"message": "Summarise my latest transactions"})
    app.state.llm = original
    after = {a["name"]: a["balance_cents"] for a in u.get("/accounts")}
    adv["compromised_llm"] = {"model_claimed": out["reply"],
                              "actual_outcomes": [f"{a['action_type']}: {a['status']} ({'; '.join(a['reasons'][:1])})"
                                                  for a in out["actions"]],
                              "balances_unchanged": before == after}

    u.step = "P4 adversarial - direct API abuse"
    direct = []
    for kind, params in [("withdraw_cash", {"amount_cents": 50000}), ("buy_investment", {"amount_cents": 100000}),
                         ("transfer_to_savings", {"amount_cents": 2_000_000})]:
        _, r = u.post("/agent/actions", {"action_type": kind, "params": params, "rationale": "abuse test"})
        direct.append(f"{kind}: {r['status']} ({'; '.join(r['reasons'][:2])})")
    s1, _ = u.call("POST", "/agent/freeze", csrf=False)
    direct.append(f"POST /agent/freeze without CSRF token -> HTTP {s1}")
    # A second, freshly signed-in session (e.g. a stolen cookie) has no step-up window.
    other = Recorder(TestClient(app), log_path, "alex-second-device")
    other.step = u.step
    _, login = other.post("/auth/login", {"email": email, "password": password}, csrf=False)
    other.csrf = login["csrf_token"]
    pol = other.get("/agent/policy")["policy"]
    for k in ("updated_at", "frozen"):
        pol.pop(k)
    s2, r2 = other.call("PUT", "/agent/policy", {**pol, "per_action_limit_cents": 100_000})
    direct.append(f"raise per-action limit from a session without re-authentication -> HTTP {s2} "
                  f"{r2['error']['code']}")
    s3, r3 = other.call("PATCH", f"/payees/{u.get('/payees')[0]['id']}", {"agent_allowlisted": True})
    direct.append(f"allowlist a biller without re-authentication -> HTTP {s3} {r3.get('error', {}).get('code', 'ok')}")
    adv["direct_api"] = direct

    u.step = "P4 adversarial - cross-tenant"
    eve = Recorder(TestClient(app), log_path, "eve")
    eve.step = u.step
    _, eme = eve.post("/auth/register", {"email": "eve@steward.demo", "password": "EveSandbox2026",
                                         "display_name": "Eve", "seed_demo": True}, csrf=False)
    eve.csrf = eme["csrf_token"]
    alex_txn = u.get("/transactions?limit=1")["items"][0]["id"]
    alex_action = u.get("/agent/actions?limit=1")[0]["id"]
    cross = []
    for method, path, body in [("GET", f"/bills/{bills[0]['id']}", None),
                               ("PATCH", f"/transactions/{alex_txn}", {"category": "Other"}),
                               ("POST", f"/agent/actions/{alex_action}/approve", None)]:
        s, _ = eve.call(method, path, body)
        cross.append(f"{method} {path} as another user -> HTTP {s}")
    eve.post("/auth/step-up", {"password": "EveSandbox2026"})
    pol = eve.get("/agent/policy")["policy"]
    for k in ("updated_at", "frozen"):
        pol.pop(k)
    eve.call("PUT", "/agent/policy", {**pol, "tier": 3})
    _, r = eve.post("/agent/actions", {"action_type": "dispute_charge", "params": {"transaction_id": alex_txn}})
    cross.append(f"Eve's agent disputes Alex's transaction -> {r['status']} ({'; '.join(r['reasons'][:1])})")
    adv["cross_tenant"] = cross

    u.step = "P4 adversarial - brute force & kill switch"
    attacker = Recorder(TestClient(app), log_path, "attacker")
    attacker.step = u.step
    codes = [attacker.post("/auth/login", {"email": email, "password": f"guess-{i}-Pass"}, csrf=False)[0]
             for i in range(7)]
    adv["brute_force_login_codes"] = codes
    _, fr = u.post("/agent/freeze")
    _, blocked = u.post("/agent/actions", {"action_type": "set_budget",
                                           "params": {"category": "Travel", "monthly_limit_cents": 10000}})
    s_unfreeze, _ = other.call("POST", "/agent/unfreeze")
    adv["kill_switch"] = {"freeze": fr, "action_while_frozen": blocked["status"],
                          "unfreeze_without_step_up_http": s_unfreeze}
    u.post("/auth/step-up", {"password": password})
    u.post("/agent/unfreeze")
    report["phases"]["P4"] = adv

    # ---------------------------------------------------------------- phase 5: integrity
    u.step = "P5 integrity"
    verify = u.get("/audit/verify")
    audit_export = u.c.get("/api/v1/audit/export.jsonl").text
    (EVIDENCE / "audit_log.jsonl").write_text(audit_export, encoding="utf-8")
    all_actions = u.get("/agent/actions?limit=500")
    (EVIDENCE / "agent_actions.json").write_text(json.dumps(all_actions, indent=2, default=str), encoding="utf-8")

    # Tamper test on a copy of the database: edit one executed payment amount in the audit log.
    tamper_db = RUN_DIR / "tampered.db"
    app.state.engine.dispose()
    shutil.copy(db_path, tamper_db)
    con = sqlite3.connect(tamper_db)
    row = con.execute("SELECT id, seq, payload FROM audit_entries WHERE user_id=1 AND event='action.executed' "
                      "ORDER BY seq LIMIT 1").fetchone()
    payload = json.loads(row[2])
    payload["amount_cents"] = 1
    con.execute("UPDATE audit_entries SET payload=? WHERE id=?", (json.dumps(payload), row[0]))
    con.commit()
    con.close()
    tampered_app = create_app(replace(settings, database_url=f"sqlite:///{tamper_db.as_posix()}"),
                              llm_provider=OfflineProvider())
    from steward.security import audit as audit_mod
    tdb = tampered_app.state.session_factory()
    tamper_result = audit_mod.verify_chain(tdb, 1)
    tdb.close()
    tampered_app.state.engine.dispose()
    integrity = {"live": verify, "tamper_test": {"edited_seq": row[1], "change": "amount_cents -> 1",
                                                "verification": tamper_result}}
    (EVIDENCE / "integrity_verification.json").write_text(json.dumps(integrity, indent=2), encoding="utf-8")
    report["phases"]["P5"] = {"audit_chain": verify["audit_chain"], "ledger_ok": verify["ledger"]["ok"],
                              "tamper_detected": not tamper_result["ok"],
                              "tamper_first_bad_seq": tamper_result["first_bad_seq"]}
    report["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report["api_calls_logged"] = sum(1 for _ in log_path.open(encoding="utf-8"))
    write_markdown(report, all_actions)
    print(json.dumps({k: v for k, v in report.items() if k != "phases"}, indent=2))
    print("Evidence written to", EVIDENCE)


def write_markdown(r: dict, actions: list[dict]) -> None:
    p = r["phases"]
    by_status: dict[str, int] = {}
    for a in actions:
        by_status[a["status"]] = by_status.get(a["status"], 0) + 1
    lines = [
        "# Execution evidence: Steward on the sandbox bank",
        "",
        f"Generated by `scripts/run_demo_scenario.py` on {r['started']} (UTC). Agent model: `{r['llm']}`. "
        f"{r['api_calls_logged']} API calls are recorded in `sandbox_operation_log.jsonl`; every agent decision "
        "is in `agent_actions.json`; the hash-chained audit trail is in `audit_log.jsonl`.",
        "",
        "All money movements happen on Steward's simulated core-banking ledger (no real funds). The run uses the "
        "same FastAPI application, policy engine, ledger and audit log as the product.",
        "",
        "## P0 · Onboarding",
        f"- Sandbox date: **{p['P0']['sandbox_date']}**, {p['P0']['transactions']} transactions of synthetic history",
        f"- Balances: {', '.join(f'{k} {v}' for k, v in p['P0']['balances'].items())}",
        f"- Financial health: {p['P0']['health']}; safe to spend until payday: {p['P0']['safe_to_spend']}",
        "",
        "## P1 · Insights and bill analysis",
        *[f"- {x}" for x in p["P1"]["insights"]],
        "",
        "Open bills and their automated audits:",
        *[f"- {x}" for x in p["P1"]["open_bills"]],
        "",
        f"Pasted-bill analyzer verdict: **{p['P1']['bill_analyzer']['status']}**, potential savings "
        f"{p['P1']['bill_analyzer']['potential_savings']}; findings: {'; '.join(p['P1']['bill_analyzer']['findings'])}.",
        "",
        "## P2 · Permission tiers",
        f"- **Tier 0 Observer**: {p['P2']['T0']['cycle_proposals']} proposals, {p['P2']['T0']['alerts']} alerts; "
        f"direct set_budget attempt → **{p['P2']['T0']['set_budget_attempt']}** ({'; '.join(p['P2']['T0']['reason'])})",
        "- **Tier 1 Organizer**:",
        *[f"  - {x}" for x in p["P2"]["T1"]["proposals"]],
        *([f"  - {p['P2']['T1']['user_approved_sweep']}"] if p["P2"]["T1"].get("user_approved_sweep") else []),
        "- **Tier 2 Assistant**:",
        *[f"  - {x}" for x in p["P2"]["T2"]["proposals"]],
        *[f"  - {x}" for x in p["P2"]["T2"]["decisions"]],
        "",
        f"## P3 · Tier 3 Autopilot: {p['P3']['days_simulated']} simulated days (to {p['P3']['sandbox_date_end']})",
        f"{p['P3']['bills_issued']} bills issued. Autonomous executions ({p['P3']['autonomous_total']} in total), each "
        "inside the per-action, daily and monthly limits and the balance floor:",
        *[f"- {x}" for x in p["P3"]["autonomous_executions"]],
        "",
        "Escalated to the user instead of executed (guardrail failed or action class needs a human):",
        *[f"- {x}" for x in p["P3"]["escalated_to_user"][:20]],
        "",
        "Approvals expire after 2 days; the agent re-raises an unanswered bill payment rather than dropping it. "
        "The simulated user only checked the app every 10 days, so the Singtel bill (held because of a new VAS "
        "charge) was paid late: the late charge then shows up on the next statement and its audit.",
        "",
        "## P4 · Adversarial tests",
        f"**Compromised model.** The scripted model replied: \"{p['P4']['compromised_llm']['model_claimed']}\". "
        "What actually happened:",
        *[f"- {x}" for x in p["P4"]["compromised_llm"]["actual_outcomes"]],
        f"- Balances unchanged: **{p['P4']['compromised_llm']['balances_unchanged']}**",
        "",
        "**Direct API abuse:**",
        *[f"- {x}" for x in p["P4"]["direct_api"]],
        "",
        "**Cross-tenant access:**",
        *[f"- {x}" for x in p["P4"]["cross_tenant"]],
        "",
        f"**Login brute force** (7 wrong passwords): HTTP {p['P4']['brute_force_login_codes']}",
        "",
        f"**Kill switch:** froze the agent and cancelled {p['P4']['kill_switch']['freeze']['cancelled_pending']} "
        f"pending action(s) (including the compromised model's held transfer); an action while frozen → "
        f"**{p['P4']['kill_switch']['action_while_frozen']}**; unfreeze from a session without re-authentication → "
        f"HTTP {p['P4']['kill_switch']['unfreeze_without_step_up_http']}.",
        "",
        "## P5 · Integrity",
        f"- Audit chain: **{'intact' if p['P5']['audit_chain']['ok'] else 'BROKEN'}**, "
        f"{p['P5']['audit_chain']['entries']} entries, head `{p['P5']['audit_chain']['head_hash']}`",
        f"- Ledger: **{'balanced' if p['P5']['ledger_ok'] else 'MISMATCH'}** (opening + postings = balance)",
        f"- Tamper test on a copy of the database (one executed amount edited): detected = "
        f"**{p['P5']['tamper_detected']}** at entry #{p['P5']['tamper_first_bad_seq']}",
        "",
        "## Agent action totals",
        "| Status | Count |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(by_status.items())],
        "",
    ]
    (EVIDENCE / "EXECUTION_EVIDENCE.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
