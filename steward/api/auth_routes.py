"""Registration, sign-in, sign-out, session info and step-up re-authentication."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from steward.agent.policy import TIERS
from steward.api.deps import Principal, client_ip, current, get_db, rate_limit
from steward.models import AgentPolicy, User
from steward.sandbox.simulator import seed_user
from steward.security import audit, sessions
from steward.security.passwords import hash_password, password_problems, verify_dummy, verify_password
from steward.services.analysis import today_for

router = APIRouter(prefix="/auth", tags=["auth"])

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")


class RegisterIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    display_name: str = Field(min_length=1, max_length=80)
    seed_demo: bool = True

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL.match(v):
            raise ValueError("Enter a valid email address.")
        return v


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class StepUpIn(BaseModel):
    password: str = Field(max_length=256)


def _set_cookie(response: Response, request: Request, token: str) -> None:
    s = request.app.state.settings
    response.set_cookie(
        sessions.COOKIE_NAME, token, httponly=True, secure=s.cookie_secure, samesite="strict",
        max_age=s.session_absolute_hours * 3600, path="/",
    )


def _me(p_user: User, sess) -> dict:
    return {
        "user": {"id": p_user.id, "email": p_user.email, "display_name": p_user.display_name,
                 "currency": p_user.currency, "sandbox_date": today_for(p_user).isoformat()},
        "csrf_token": sess.csrf_token,
        "step_up_active": sessions.step_up_active(sess),
    }


@router.post("/register", status_code=201)
def register(body: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    settings = request.app.state.settings
    if not settings.allow_registration:
        raise HTTPException(403, {"code": "registration_closed", "message": "Registration is disabled."})
    rate_limit(request, f"register:{client_ip(request)}", 10, 3600)
    problems = password_problems(body.password, body.email)
    if problems:
        raise HTTPException(422, {"code": "weak_password", "message": " ".join(problems)})
    if db.execute(select(User.id).where(User.email == body.email)).first():
        raise HTTPException(409, {"code": "email_taken", "message": "An account with this email already exists."})
    user = User(email=body.email, display_name=body.display_name.strip(), password_hash=hash_password(body.password),
                currency=settings.currency)
    db.add(user)
    db.flush()
    if body.seed_demo:
        from datetime import date
        seed_user(db, user, seed=settings.demo_seed + user.id, anchor=date.today())
    else:
        db.add(AgentPolicy(user_id=user.id, tier=1))
    audit.append(db, user.id, "user", "auth.registered", {"seed_demo": body.seed_demo})
    token, sess = sessions.create_session(db, user, settings, client_ip(request))
    db.commit()
    _set_cookie(response, request, token)
    return _me(user, sess)


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    email = body.email.strip().lower()
    ip = client_ip(request)
    rate_limit(request, f"login-ip:{ip}", 20, 300)
    rate_limit(request, f"login-email:{email}", 5, 300)
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        verify_dummy(body.password)
        raise HTTPException(401, {"code": "bad_credentials", "message": "Email or password is incorrect."})
    if not verify_password(body.password, user.password_hash):
        audit.append(db, user.id, "user", "auth.login_failed", {"ip": ip})
        db.commit()
        raise HTTPException(401, {"code": "bad_credentials", "message": "Email or password is incorrect."})
    token, sess = sessions.create_session(db, user, request.app.state.settings, ip)
    audit.append(db, user.id, "user", "auth.login", {"ip": ip})
    db.commit()
    request.app.state.limiter.reset(f"login-email:{email}")
    _set_cookie(response, request, token)
    return _me(user, sess)


@router.post("/logout")
def logout(response: Response, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    sess = db.merge(p.session)
    sessions.revoke(db, sess)
    audit.append(db, p.user.id, "user", "auth.logout", {})
    db.commit()
    response.delete_cookie(sessions.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(p: Principal = Depends(current)) -> dict:
    out = _me(p.user, p.session)
    out["tiers"] = TIERS
    return out


@router.post("/step-up")
def step_up(body: StepUpIn, request: Request, p: Principal = Depends(current), db: Session = Depends(get_db)) -> dict:
    rate_limit(request, f"stepup:{p.user.id}", 5, 300)
    sess = db.merge(p.session)
    if not verify_password(body.password, p.user.password_hash):
        audit.append(db, p.user.id, "user", "auth.step_up_failed", {})
        db.commit()
        raise HTTPException(401, {"code": "bad_credentials", "message": "Password is incorrect."})
    until = sessions.grant_step_up(sess, request.app.state.settings)
    audit.append(db, p.user.id, "user", "auth.step_up", {"until": until.isoformat()})
    db.commit()
    return {"step_up_until": until.isoformat()}
