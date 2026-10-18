"""Request dependencies: database session, authenticated user, CSRF and step-up checks."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from steward.config import Settings
from steward.models import AuthSession, User
from steward.security import audit, sessions

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    db = request.app.state.session_factory()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def client_ip(request: Request) -> str:
    # Behind a trusted proxy, configure uvicorn --proxy-headers instead of reading X-Forwarded-For here.
    return request.client.host if request.client else "unknown"


@dataclass
class Principal:
    user: User
    session: AuthSession


def current(request: Request, db: Session = Depends(get_db)) -> Principal:
    settings: Settings = request.app.state.settings
    sess = sessions.resolve(db, request.cookies.get(sessions.COOKIE_NAME), settings)
    if sess is None:
        raise HTTPException(status_code=401, detail={"code": "not_authenticated", "message": "Please sign in."})
    if request.method in UNSAFE_METHODS and not sessions.csrf_ok(sess, request.headers.get("x-csrf-token")):
        raise HTTPException(status_code=403, detail={"code": "csrf_failed", "message": "Missing or invalid CSRF token."})
    db.commit()  # persist last_seen_at (sliding idle timeout)
    if request.method in UNSAFE_METHODS:
        # One writer per user (PostgreSQL): balances, limits and the sandbox clock are read after
        # the previous write committed, and every request takes its locks in the same order.
        audit.lock_user(db, sess.user_id)
    user = db.get(User, sess.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "not_authenticated", "message": "Please sign in."})
    return Principal(user=user, session=sess)


def require_step_up(p: Principal) -> None:
    if not sessions.step_up_active(p.session):
        raise HTTPException(status_code=401, detail={
            "code": "step_up_required", "message": "Confirm your password to continue."})


def rate_limit(request: Request, key: str, limit: int, window: float) -> None:
    if not request.app.state.limiter.hit(key, limit, window):
        raise HTTPException(status_code=429, detail={"code": "rate_limited",
                                                     "message": "Too many requests. Try again shortly."})
