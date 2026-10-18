"""Agent permissions (tier, limits, kill switch), action queue, chat, audit and sandbox."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from steward.agent import chat as chat_mod
from steward.agent.cycle import run_cycle
from steward.agent.policy import ACTION_RISK, HARD_CEILING_CENTS, TIER_MATRIX, TIERS
from steward.agent.service import ActionError, AgentService, action_to_dict
from steward.api.deps import Principal, current, get_db, rate_limit, require_step_up
from steward.models import (
    Account,
    AgentAction,
    AgentPolicy,
    Alert,
    AuditEntry,
    Bill,
    Budget,
    ChatMessage,
    Dispute,
    Goal,
    MerchantMemory,
    Payee,
    SandboxProfile,
    SubscriptionState,
    Transaction,
)
from steward.sandbox import bank, simulator
from steward.security import audit, sessions
from steward.services.analysis import today_for

router = APIRouter(tags=["agent"])


def _err(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def policy_view(pol: AgentPolicy) -> dict:
    return {
        "tier": pol.tier, "frozen": pol.frozen, "per_action_limit_cents": pol.per_action_limit_cents,
        "daily_limit_cents": pol.daily_limit_cents, "monthly_limit_cents": pol.monthly_limit_cents,
        "min_balance_floor_cents": pol.min_balance_floor_cents, "step_up_threshold_cents": pol.step_up_threshold_cents,
        "updated_at": pol.updated_at.isoformat() if pol.updated_at else None,
    }


# -------------------------------------------------------------------- policy
@router.get("/agent/policy")
def get_policy(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    pol = AgentService(db, p.user).policy
    db.commit()
    matrix = {t: {r: o.value for r, o in row.items()} for t, row in TIER_MATRIX.items()}
    return {"policy": policy_view(pol), "tiers": TIERS, "matrix": matrix, "action_risk": ACTION_RISK,
            "hard_ceiling_cents": HARD_CEILING_CENTS}


class PolicyIn(BaseModel):
    tier: int = Field(ge=0, le=3)
    per_action_limit_cents: int = Field(ge=0, le=HARD_CEILING_CENTS)
    daily_limit_cents: int = Field(ge=0, le=5_000_000)
    monthly_limit_cents: int = Field(ge=0, le=20_000_000)
    min_balance_floor_cents: int = Field(ge=0, le=100_000_000)
    step_up_threshold_cents: int = Field(ge=1_000, le=HARD_CEILING_CENTS)


@router.put("/agent/policy")
def put_policy(body: PolicyIn, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    pol = AgentService(db, p.user).policy
    widening = (
        body.tier > pol.tier
        or body.per_action_limit_cents > pol.per_action_limit_cents
        or body.daily_limit_cents > pol.daily_limit_cents
        or body.monthly_limit_cents > pol.monthly_limit_cents
        or body.min_balance_floor_cents < pol.min_balance_floor_cents
        or body.step_up_threshold_cents > pol.step_up_threshold_cents
    )
    if widening:
        require_step_up(p)  # granting the agent more power needs a fresh password check
    if body.daily_limit_cents < body.per_action_limit_cents or body.monthly_limit_cents < body.daily_limit_cents:
        raise _err(422, "inconsistent_limits", "Limits must satisfy per-action ≤ daily ≤ monthly.")
    before = policy_view(pol)
    for k, v in body.model_dump().items():
        setattr(pol, k, v)
    pol.updated_at = datetime.now(timezone.utc)
    audit.append(db, p.user.id, "user", "policy.updated", {"before": before, "after": body.model_dump(),
                                                          "widening": widening})
    db.commit()
    return {"policy": policy_view(pol)}


@router.post("/agent/freeze")
def freeze(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    """Kill switch: immediately stops all agent actions (no step-up needed to make things safer)."""
    svc = AgentService(db, p.user)
    svc.policy.frozen = True
    pending = db.execute(select(AgentAction).where(AgentAction.user_id == p.user.id,
                                                   AgentAction.status == "pending_approval")).scalars().all()
    for a in pending:
        a.status = "rejected"
        a.result = {"note": "Cancelled by kill switch."}
    audit.append(db, p.user.id, "user", "agent.frozen", {"cancelled_pending": len(pending)})
    db.commit()
    return {"frozen": True, "cancelled_pending": len(pending)}


@router.post("/agent/unfreeze")
def unfreeze(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    require_step_up(p)
    svc = AgentService(db, p.user)
    svc.policy.frozen = False
    audit.append(db, p.user.id, "user", "agent.unfrozen", {})
    db.commit()
    return {"frozen": False}


# ------------------------------------------------------------------- actions
@router.post("/agent/cycle")
def cycle(request: Request, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    rate_limit(request, f"cycle:{p.user.id}", 30, 60)
    report = run_cycle(db, p.user)
    db.commit()
    return report


@router.get("/agent/actions")
def list_actions(status: str | None = Query(None, max_length=20), limit: int = Query(100, ge=1, le=500),
                 p: Principal = Depends(current), db: Session = Depends(get_db)) -> list[dict]:
    stmt = select(AgentAction).where(AgentAction.user_id == p.user.id)
    if status:
        stmt = stmt.where(AgentAction.status == status)
    rows = db.execute(stmt.order_by(AgentAction.id.desc()).limit(limit)).scalars()
    return [action_to_dict(a) for a in rows]


class ActionIn(BaseModel):
    action_type: str = Field(max_length=40)
    params: dict = Field(default_factory=dict)
    rationale: str = Field(default="", max_length=500)


@router.post("/agent/actions", status_code=201)
def create_action(body: ActionIn, request: Request, p: Principal = Depends(current),
                  db: Session = Depends(get_db)) -> dict:
    """One-click actions from insights: the user asks the agent to do something. Same policy as any agent action."""
    rate_limit(request, f"actions:{p.user.id}", 30, 60)
    if len(json.dumps(body.params)) > 2000:
        raise _err(422, "params_too_large", "Parameters are too large.")
    action = AgentService(db, p.user).propose(body.action_type, body.params, rationale=body.rationale,
                                              origin="insight")
    db.commit()
    return action_to_dict(action)


@router.post("/agent/actions/{action_id}/approve")
def approve(action_id: int, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    svc = AgentService(db, p.user)
    try:
        action = svc.approve(action_id, step_up_ok=sessions.step_up_active(p.session))
    except ActionError as exc:
        db.commit()  # keep e.g. the 'expired' transition
        code = "step_up_required" if exc.status_code == 401 else "action_error"
        raise _err(exc.status_code, code, str(exc)) from exc
    db.commit()
    return action_to_dict(action)


@router.post("/agent/actions/{action_id}/reject")
def reject(action_id: int, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    try:
        action = AgentService(db, p.user).reject(action_id)
    except ActionError as exc:
        raise _err(exc.status_code, "action_error", str(exc)) from exc
    db.commit()
    return action_to_dict(action)


# ---------------------------------------------------------------------- chat
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(None, max_length=40, pattern=r"^[A-Za-z0-9-]+$")


@router.post("/agent/chat")
def chat(body: ChatIn, request: Request, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    rate_limit(request, f"chat:{p.user.id}", 20, 60)
    out = chat_mod.chat(db, p.user, body.message, body.conversation_id, request.app.state.llm)
    db.commit()
    return out


@router.get("/agent/chat/{conversation_id}")
def chat_history(conversation_id: str, p: Principal = Depends(current), db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(ChatMessage).where(ChatMessage.user_id == p.user.id,
                                                ChatMessage.conversation_id == conversation_id[:40])
                      .order_by(ChatMessage.id)).scalars()
    return [{"role": m.role, "content": m.content, "meta": m.meta} for m in rows]


# --------------------------------------------------------------------- audit
@router.get("/audit")
def audit_log(limit: int = Query(200, ge=1, le=1000), p: Principal = Depends(current),
              db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(AuditEntry).where(AuditEntry.user_id == p.user.id)
                      .order_by(AuditEntry.seq.desc()).limit(limit)).scalars()
    return [{"seq": e.seq, "ts": e.ts, "actor": e.actor, "event": e.event, "payload": e.payload,
             "prev_hash": e.prev_hash, "hash": e.hash} for e in rows]


@router.get("/audit/verify")
def audit_verify(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    return {"audit_chain": audit.verify_chain(db, p.user.id), "ledger": bank.verify_ledger(db, p.user.id)}


@router.get("/audit/export.jsonl")
def audit_export(p: Principal = Depends(current), db: Session = Depends(get_db)) -> StreamingResponse:
    rows = db.execute(select(AuditEntry).where(AuditEntry.user_id == p.user.id).order_by(AuditEntry.seq)).scalars()
    body = "".join(json.dumps({"seq": e.seq, "ts": e.ts, "actor": e.actor, "event": e.event, "payload": e.payload,
                               "prev_hash": e.prev_hash, "hash": e.hash}, sort_keys=True) + "\n" for e in rows)
    return StreamingResponse(iter([body]), media_type="application/x-ndjson",
                             headers={"Content-Disposition": 'attachment; filename="steward-audit.jsonl"'})


# ------------------------------------------------------------------- sandbox
@router.get("/sandbox")
def sandbox_status(p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    prof = db.get(SandboxProfile, p.user.id)
    return {"sandbox_date": today_for(p.user).isoformat(), "seeded": prof is not None,
            "anchor": prof.anchor.isoformat() if prof else None, "ledger": bank.verify_ledger(db, p.user.id)}


class AdvanceIn(BaseModel):
    days: int = Field(ge=1, le=90)
    run_agent: bool = True


@router.post("/sandbox/advance")
def sandbox_advance(body: AdvanceIn, request: Request, p: Principal = Depends(current),
                    db: Session = Depends(get_db)) -> dict:
    rate_limit(request, f"advance:{p.user.id}", 20, 60)
    if db.get(SandboxProfile, p.user.id) is None:
        raise _err(409, "no_sandbox", "This account has no sandbox persona.")
    log = simulator.advance(db, p.user, body.days, run_cycle if body.run_agent else None)
    db.commit()
    return {"sandbox_date": today_for(p.user).isoformat(), "days": log}


@router.post("/sandbox/reset")
def sandbox_reset(request: Request, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    rate_limit(request, f"reset:{p.user.id}", 5, 3600)
    uid = p.user.id
    for model in (ChatMessage, AgentAction, Alert, Dispute, SubscriptionState, Bill, Budget, Goal, MerchantMemory,
                  Transaction, Payee, Account, AgentPolicy, SandboxProfile):
        db.execute(delete(model).where(model.user_id == uid))
    db.flush()
    from datetime import date
    simulator.seed_user(db, p.user, seed=request.app.state.settings.demo_seed + uid, anchor=date.today())
    # The audit chain is never deleted: the reset itself is recorded on it.
    audit.append(db, uid, "user", "sandbox.reset", {})
    db.commit()
    return {"ok": True, "sandbox_date": today_for(p.user).isoformat()}
