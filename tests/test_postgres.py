"""Concurrency on PostgreSQL: several workers and threads writing for one user at once.

These run only when STEWARD_TEST_POSTGRES_URL is set (see conftest.py); SQLite has a single
writer, so the races they cover cannot happen there."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from sqlalchemy import select

from steward.db import init_db, make_engine, make_session_factory
from steward.models import AgentAction
from steward.security import audit
from tests.conftest import postgres_only

pytestmark = postgres_only


def _together(n, fn):
    """Run fn(i) for i in range(n) on n threads released at the same moment; return the results."""
    gate = threading.Barrier(n, timeout=30)

    def run(i):
        gate.wait()
        return fn(i)

    with ThreadPoolExecutor(n) as pool:
        return [f.result(timeout=60) for f in [pool.submit(run, i) for i in range(n)]]


def test_parallel_workers_create_the_schema_once(database_url):
    engines = [make_engine(database_url) for _ in range(4)]
    try:
        _together(4, lambda i: init_db(engines[i]))  # used to fail: duplicate key in pg_class
    finally:
        for e in engines:
            e.dispose()


def test_interleaved_audit_appends_do_not_deadlock(database_url):
    """Each transaction appends, pauses, appends again. With an in-process lock released before
    commit this deadlocked: one thread held the advisory lock and waited for the in-process lock,
    the other held the in-process lock and waited for the advisory lock."""
    engine = make_engine(database_url)
    init_db(engine)
    factory = make_session_factory(engine)

    def two_appends(i):
        db = factory()
        try:
            audit.append(db, 7, "agent", "first", {"i": i})
            threading.Event().wait(0.05)
            audit.append(db, 7, "agent", "second", {"i": i})
            db.commit()
        finally:
            db.close()

    _together(8, two_appends)
    db = factory()
    v = audit.verify_chain(db, 7)
    db.close()
    engine.dispose()
    assert v["ok"] and v["entries"] == 16


def test_concurrent_sweeps_cannot_breach_the_balance_floor(user):
    user.set_policy(tier=3, per_action_limit_cents=50_000, daily_limit_cents=500_000, monthly_limit_cents=2_000_000)
    checking = next(a for a in user.get("/accounts").json() if a["type"] == "checking")
    # Room above the floor for one S$300 sweep, not two.
    user.set_policy(min_balance_floor_cents=checking["balance_cents"] - 45_000)

    def sweep(i):
        r = user.post("/agent/actions", {"action_type": "transfer_to_savings", "params": {"amount_cents": 30_000 + i}})
        return r.status_code, r.json()["status"]

    results = _together(6, sweep)
    assert all(code == 201 for code, _ in results), results
    assert sorted(s for _, s in results) == ["denied"] * 5 + ["executed"]
    after = next(a for a in user.get("/accounts").json() if a["type"] == "checking")
    floor = user.get("/agent/policy").json()["policy"]["min_balance_floor_cents"]
    assert after["balance_cents"] >= floor
    v = user.get("/audit/verify").json()
    assert v["audit_chain"]["ok"] and v["ledger"]["ok"]


def test_double_submitted_action_runs_once(user, app):
    """Identical proposals at the same moment return the same action instead of a 500 on the
    idempotency constraint, and the money moves once."""
    user.set_policy(tier=3)
    body = {"action_type": "transfer_to_savings", "params": {"amount_cents": 1_234}}
    results = _together(5, lambda i: user.post("/agent/actions", body))
    assert [r.status_code for r in results] == [201] * 5
    assert len({r.json()["id"] for r in results}) == 1
    with app.state.session_factory() as db:
        rows = db.execute(select(AgentAction).where(AgentAction.amount_cents == 1_234)).scalars().all()
    assert [a.status for a in rows] == ["executed"]


def test_advance_and_actions_together_keep_the_ledger_balanced(user):
    """The sandbox clock, agent sweeps and reminders for one user, all at once."""
    user.set_policy(tier=3)

    def work(i):
        if i % 3 == 0:
            return user.post("/sandbox/advance", {"days": 2, "run_agent": True}).status_code
        if i % 3 == 1:
            return user.post("/agent/actions", {"action_type": "transfer_to_savings",
                                                "params": {"amount_cents": 500 + i}}).status_code
        return user.post("/agent/actions", {"action_type": "create_reminder",
                                            "params": {"title": f"check {i}"}}).status_code

    before = user.get("/sandbox").json()["sandbox_date"]
    codes = _together(9, work)
    assert set(codes) <= {200, 201}, codes
    after = user.get("/sandbox").json()
    # Three advances of two days each: every one must start from the date the previous one left.
    assert after["sandbox_date"] == (date.fromisoformat(before) + timedelta(days=6)).isoformat()
    v = user.get("/audit/verify").json()
    assert v["audit_chain"]["ok"] and v["ledger"]["ok"], v
