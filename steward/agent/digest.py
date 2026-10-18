"""Weekly money digest with grounded generation.

Facts come from the deterministic engines. The LLM (when configured) only rewrites them in
a friendly voice; every amount and percentage in its output must already appear in the
facts, otherwise the draft is discarded and the template digest is used instead."""

from __future__ import annotations

import re

from steward.agent.llm import AnthropicProvider, LLMError
from steward.core.money import fmt
from steward.security.untrusted import for_llm
from steward.services.analysis import Analysis

NUMBER = re.compile(r"S\$\s?[\d,]+(?:\.\d{2})?|\d+(?:\.\d+)?%")

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "tips": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "summary", "tips"],
    "additionalProperties": False,
}

DIGEST_SYSTEM = (
    "You write a short weekly money digest for one person. Use only the facts provided; do not add numbers "
    "that are not in the facts, and copy amounts exactly as written. Warm, plain English, no jargon. "
    "headline: under 12 words. summary: 2-3 sentences. tips: 2-3 concrete next steps."
)


def _numbers(text: str) -> set[str]:
    return {n.replace(" ", "") for n in NUMBER.findall(text)}


def facts_for(a: Analysis) -> list[str]:
    facts = [f"{i.title}. {i.body}" for i in a.insights[:7]]
    h = a.health
    facts.append(f"Financial health score {h['score']}/100 (grade {h['grade']}).")
    facts.append(f"Safe to spend until payday: {fmt(a.forecast.safe_to_spend_cents, a.currency)}.")
    return [for_llm(f, 400) for f in facts]


def template_digest(a: Analysis, facts: list[str]) -> dict:
    top = [i for i in a.insights if i.severity in {"critical", "warning"}]
    headline = (f"{len(top)} thing(s) need your attention this week" if top else "A calm week for your money")
    return {"headline": headline, "summary": " ".join(facts[:3]),
            "tips": [i.suggested_action["label"] for i in a.insights if i.suggested_action][:3]
            or ["Review your budgets on the Budgets page."]}


def weekly_digest(a: Analysis, provider) -> dict:
    facts = facts_for(a)
    base = template_digest(a, facts)
    if not isinstance(provider, AnthropicProvider):
        return {**base, "source": "template", "grounding_check": "n/a", "facts": facts}
    allowed = set().union(*(_numbers(f) for f in facts))
    try:
        draft = provider.complete_json(DIGEST_SYSTEM, "Facts:\n- " + "\n- ".join(facts), DIGEST_SCHEMA)
    except (LLMError, ValueError):
        return {**base, "source": "template", "grounding_check": "llm_unavailable", "facts": facts}
    used = _numbers(" ".join([draft.get("headline", ""), draft.get("summary", ""), *draft.get("tips", [])]))
    unknown = sorted(used - allowed)
    if unknown:
        return {**base, "source": "template", "grounding_check": f"rejected: ungrounded numbers {unknown}",
                "facts": facts}
    return {"headline": draft["headline"][:120], "summary": draft["summary"][:800],
            "tips": [t[:200] for t in draft["tips"][:3]], "source": "llm", "grounding_check": "passed",
            "facts": facts}
