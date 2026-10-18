"""HTTP-level security: authentication, sessions, CSRF, step-up, rate limits, headers and
tenant isolation."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from steward.models import AuthSession
from tests.conftest import PASSWORD, Session, register


def test_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/healthz").json()["status"] == "ok"


def test_register_sets_hardened_cookie_and_hides_password(client):
    r = client.post("/api/v1/auth/register", json={"email": "A@Example.com", "password": PASSWORD,
                                                   "display_name": "A", "seed_demo": False})
    assert r.status_code == 201
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert PASSWORD not in r.text and r.json()["user"]["email"] == "a@example.com"


def test_weak_password_and_duplicate_email(client):
    r = client.post("/api/v1/auth/register", json={"email": "b@example.com", "password": "short", "display_name": "B"})
    assert r.status_code == 422
    register(client, "c@example.com", seed_demo=False)
    r = client.post("/api/v1/auth/register", json={"email": "c@example.com", "password": PASSWORD, "display_name": "C"})
    assert r.status_code == 409


def test_unauthenticated_requests_rejected(client):
    for path in ("/overview", "/transactions", "/agent/actions", "/audit", "/bills"):
        r = client.get("/api/v1" + path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "not_authenticated"


def test_login_logout_and_generic_errors(app):
    c1 = TestClient(app)
    register(c1, "d@example.com", seed_demo=False)
    c2 = TestClient(app)
    bad_user = c2.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    bad_pass = c2.post("/api/v1/auth/login", json={"email": "d@example.com", "password": "Wrong-Pass-123"})
    assert bad_user.status_code == bad_pass.status_code == 401
    assert bad_user.json() == bad_pass.json()  # no account enumeration
    ok = c2.post("/api/v1/auth/login", json={"email": "d@example.com", "password": PASSWORD})
    assert ok.status_code == 200
    s = Session(c2, ok.json())
    assert s.post("/auth/logout").status_code == 200
    assert c2.get("/api/v1/overview").status_code == 401


def test_login_brute_force_is_rate_limited(app):
    c = TestClient(app)
    register(c, "e@example.com", seed_demo=False)
    codes = [c.post("/api/v1/auth/login", json={"email": "e@example.com", "password": f"Wrong-{i}-pass"}).status_code
             for i in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
    # Even the right password is refused while the lock is active.
    assert c.post("/api/v1/auth/login", json={"email": "e@example.com", "password": PASSWORD}).status_code == 429


def test_csrf_required_for_state_changes(user):
    no_header = user.c.post("/api/v1/agent/freeze")
    wrong = user.c.post("/api/v1/agent/freeze", headers={"x-csrf-token": "forged"})
    assert no_header.status_code == wrong.status_code == 403
    assert user.post("/agent/freeze").status_code == 200


def test_idle_session_expires(app, user):
    db = app.state.session_factory()
    for s in db.execute(select(AuthSession)).scalars():
        s.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    db.commit()
    db.close()
    assert user.get("/overview").status_code == 401


def test_raising_tier_requires_step_up_but_lowering_does_not(user):
    pol = user.get("/agent/policy").json()["policy"]
    for k in ("updated_at", "frozen"):
        pol.pop(k)
    r = user.put("/agent/policy", {**pol, "tier": 3})
    assert r.status_code == 401 and r.json()["error"]["code"] == "step_up_required"
    r = user.put("/agent/policy", {**pol, "per_action_limit_cents": pol["per_action_limit_cents"] + 1})
    assert r.status_code == 401
    assert user.put("/agent/policy", {**pol, "tier": 0}).status_code == 200  # safer: no password needed
    assert user.post("/auth/step-up", {"password": "Wrong-pass-1"}).status_code == 401
    user.step_up()
    assert user.put("/agent/policy", {**pol, "tier": 3}).status_code == 200


def test_policy_limits_must_be_consistent(user):
    user.step_up()
    pol = user.get("/agent/policy").json()["policy"]
    for k in ("updated_at", "frozen"):
        pol.pop(k)
    r = user.put("/agent/policy", {**pol, "daily_limit_cents": pol["per_action_limit_cents"] - 1})
    assert r.status_code == 422
    r = user.put("/agent/policy", {**pol, "per_action_limit_cents": 10**9})
    assert r.status_code == 422  # above the hard ceiling


def test_unfreeze_and_allowlisting_need_step_up(user):
    assert user.post("/agent/freeze").status_code == 200
    assert user.post("/agent/unfreeze").status_code == 401
    payee = next(p for p in user.get("/payees").json() if not p["agent_allowlisted"])
    assert user.patch(f"/payees/{payee['id']}", {"agent_allowlisted": True}).status_code == 401
    user.step_up()
    assert user.post("/agent/unfreeze").status_code == 200
    assert user.patch(f"/payees/{payee['id']}", {"agent_allowlisted": True}).status_code == 200


def test_tenant_isolation(app):
    alice = register(TestClient(app), "alice@example.com")
    bob = register(TestClient(app), "bob@example.com")
    a_txn = alice.get("/transactions?limit=1").json()["items"][0]
    a_bill = alice.get("/bills").json()["open"][0]
    a_goal = alice.get("/goals").json()["goals"][0]
    alice.set_policy(tier=2)
    a_action = alice.post("/agent/actions", {"action_type": "pay_bill", "params": {"bill_id": a_bill["id"]}}).json()
    assert a_action["status"] == "pending_approval"

    # Bob can neither read nor touch Alice's data, and gets 404s (no existence oracle).
    assert all(t["id"] != a_txn["id"] for t in bob.get("/transactions?limit=500").json()["items"])
    assert bob.patch(f"/transactions/{a_txn['id']}", {"category": "Other"}).status_code == 404
    assert bob.get(f"/bills/{a_bill['id']}").status_code == 404
    assert bob.post(f"/bills/{a_bill['id']}/pay").status_code == 404
    assert bob.delete(f"/goals/{a_goal['id']}").status_code == 404
    assert bob.post(f"/agent/actions/{a_action['id']}/approve").status_code == 404
    assert bob.post(f"/agent/actions/{a_action['id']}/reject").status_code == 404
    # Even through the agent: Bob's agent cannot pay Alice's bill or dispute Alice's charge.
    bob.set_policy(tier=3)
    r = bob.post("/agent/actions", {"action_type": "pay_bill", "params": {"bill_id": a_bill["id"]}}).json()
    assert r["status"] == "denied"
    r = bob.post("/agent/actions", {"action_type": "dispute_charge", "params": {"transaction_id": a_txn["id"]}}).json()
    assert r["status"] == "denied"
    assert alice.get("/agent/actions").json()[0]["status"] == "pending_approval"
    assert bob.get("/audit/verify").json()["audit_chain"]["ok"]


def test_validation_errors_are_clean(user):
    r = user.post("/affordability", {"amount_cents": -5})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    r = user.post("/agent/chat", {"message": ""})
    assert r.status_code == 422
