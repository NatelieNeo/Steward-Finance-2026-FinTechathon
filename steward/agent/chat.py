"""Conversational agent: stores the conversation, runs the provider over the Toolbox, and
audits which tools ran and which actions were proposed."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from steward.agent.llm import SYSTEM_PROMPT, AnthropicProvider, LLMError, OfflineProvider
from steward.agent.tools import TOOL_SCHEMAS, Toolbox
from steward.models import ChatMessage, User
from steward.security import audit
from steward.security.untrusted import redact_pii

MAX_MESSAGE_CHARS = 2000
HISTORY_TURNS = 10


def chat(db: Session, user: User, message: str, conversation_id: str | None, provider) -> dict:
    conversation_id = conversation_id or str(uuid.uuid4())
    message = message.strip()[:MAX_MESSAGE_CHARS]
    # Data minimisation: the model never needs card numbers, IDs or contact details.
    safe_message = redact_pii(message)

    prior = db.execute(
        select(ChatMessage).where(ChatMessage.user_id == user.id, ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.id.desc()).limit(HISTORY_TURNS * 2)
    ).scalars().all()
    history = [{"role": m.role, "content": m.content} for m in reversed(prior)]

    toolbox = Toolbox(db, user, conversation_id)
    note = None
    try:
        if isinstance(provider, AnthropicProvider):
            result = provider.run(SYSTEM_PROMPT, history + [{"role": "user", "content": safe_message}],
                                  TOOL_SCHEMAS, toolbox.execute)
        else:
            result = provider.run_message(safe_message, toolbox.execute)
    except LLMError as exc:
        result = OfflineProvider().run_message(safe_message, toolbox.execute)
        note = f"{exc} Answered in offline mode."
    note = note or result.note

    db.add(ChatMessage(user_id=user.id, conversation_id=conversation_id, role="user", content=safe_message))
    db.add(ChatMessage(user_id=user.id, conversation_id=conversation_id, role="assistant", content=result.text,
                       meta={"provider": result.provider, "tools": [c["name"] for c in result.tool_calls],
                             "actions": [a["id"] for a in toolbox.actions]}))
    audit.append(db, user.id, "agent", "agent.chat", {
        "conversation_id": conversation_id, "provider": result.provider, "model": result.model,
        "tools": [c["name"] for c in result.tool_calls],
        "actions": [{"id": a["id"], "type": a["action_type"], "status": a["status"]} for a in toolbox.actions],
    })
    return {
        "conversation_id": conversation_id,
        "reply": result.text,
        "provider": result.provider,
        "model": result.model,
        "note": note,
        "tool_calls": [{"name": c["name"], "input": c["input"]} for c in result.tool_calls],
        "actions": toolbox.actions,
    }
