"""Grounded generation: LLM digests may only use numbers that the engines produced."""

from steward.agent.digest import _numbers, facts_for, weekly_digest
from steward.agent.llm import AnthropicProvider
from steward.models import User
from steward.services.analysis import Analysis
from tests.conftest import register


class FakeClaude(AnthropicProvider):
    def __init__(self, draft):  # no SDK client: never touches the network
        self.draft = draft
        self.model = "fake"

    def complete_json(self, system, prompt, schema):
        self.prompt = prompt
        return self.draft


def _analysis(app, client):
    register(client)
    db = app.state.session_factory()
    return Analysis(db, db.query(User).one())


def test_number_extraction():
    assert _numbers("Spent S$1,234.50 (12% more), saved S$ 40") == {"S$1,234.50", "12%", "S$40"}


def test_template_digest_without_llm(app, client):
    from steward.agent.llm import OfflineProvider
    d = weekly_digest(_analysis(app, client), OfflineProvider())
    assert d["source"] == "template" and d["headline"] and d["facts"]


def test_grounded_llm_digest_is_accepted(app, client):
    a = _analysis(app, client)
    fact_number = sorted(set().union(*(_numbers(f) for f in facts_for(a))))[0]
    fake = FakeClaude({"headline": "Your week", "summary": f"Heads up: {fact_number} stands out.", "tips": ["Review it"]})
    d = weekly_digest(a, fake)
    assert d["source"] == "llm" and d["grounding_check"] == "passed"
    assert "IGNORE PREVIOUS" not in fake.prompt  # quarantined text never reaches the model


def test_hallucinated_numbers_are_rejected(app, client):
    fake = FakeClaude({"headline": "You saved S$9,999.99!", "summary": "Up 73%.", "tips": []})
    d = weekly_digest(_analysis(app, client), fake)
    assert d["source"] == "template" and d["grounding_check"].startswith("rejected")
