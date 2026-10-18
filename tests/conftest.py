"""Shared fixtures. Every test app gets its own temporary SQLite database and the offline
LLM provider, so the suite needs no network and no API key.

Set STEWARD_TEST_POSTGRES_URL (a server URL whose user may create databases, e.g.
postgresql+psycopg://steward:pw@localhost:5432/postgres) to run the same suite against a
fresh PostgreSQL database per test instead."""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("STEWARD_LLM_PROVIDER", "offline")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, make_url, text  # noqa: E402

from steward.agent.llm import OfflineProvider  # noqa: E402
from steward.config import get_settings  # noqa: E402
from steward.main import create_app  # noqa: E402

PASSWORD = "SandboxTest2026"
POSTGRES_URL = os.environ.get("STEWARD_TEST_POSTGRES_URL")

postgres_only = pytest.mark.skipif(not POSTGRES_URL, reason="set STEWARD_TEST_POSTGRES_URL to run against PostgreSQL")


@pytest.fixture
def database_url(tmp_path):
    if not POSTGRES_URL:
        yield f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
        return
    name = f"steward_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    # lock_timeout turns a lock-ordering regression into a failing test instead of a hung suite.
    url = make_url(POSTGRES_URL).set(database=name).update_query_dict({"options": "-c lock_timeout=20000"})
    yield url.render_as_string(hide_password=False)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def make_app(database_url):
    apps = []

    def _make(provider=None, **overrides):
        settings = replace(get_settings(), database_url=database_url, llm_provider="offline", **overrides)
        apps.append(create_app(settings, llm_provider=provider or OfflineProvider()))
        return apps[-1]
    yield _make
    for a in apps:  # close pooled connections before the test database is dropped
        a.state.engine.dispose()


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return TestClient(app)


class Session:
    """A signed-in user: wraps the TestClient with the CSRF header."""

    def __init__(self, client: TestClient, me: dict):
        self.c = client
        self.me = me
        self.h = {"x-csrf-token": me["csrf_token"]}

    def get(self, path, **kw):
        return self.c.get("/api/v1" + path, **kw)

    def post(self, path, json=None, **kw):
        return self.c.post("/api/v1" + path, json=json, headers=self.h, **kw)

    def put(self, path, json=None):
        return self.c.put("/api/v1" + path, json=json, headers=self.h)

    def patch(self, path, json=None):
        return self.c.patch("/api/v1" + path, json=json, headers=self.h)

    def delete(self, path):
        return self.c.delete("/api/v1" + path, headers=self.h)

    def step_up(self):
        r = self.post("/auth/step-up", {"password": PASSWORD})
        assert r.status_code == 200, r.text

    def set_policy(self, **changes):
        self.step_up()
        pol = self.get("/agent/policy").json()["policy"]
        for k in ("updated_at", "frozen"):
            pol.pop(k)
        pol.update(changes)
        r = self.put("/agent/policy", pol)
        assert r.status_code == 200, r.text
        return r.json()["policy"]


def register(client: TestClient, email: str = "alex@example.com", seed_demo: bool = True) -> Session:
    r = client.post("/api/v1/auth/register",
                    json={"email": email, "password": PASSWORD, "display_name": "Alex", "seed_demo": seed_demo})
    assert r.status_code == 201, r.text
    return Session(client, r.json())


@pytest.fixture
def user(client) -> Session:
    return register(client)
