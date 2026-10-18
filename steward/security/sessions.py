"""Server-side sessions: opaque random token in an HttpOnly cookie, only its SHA-256 stored.

Sessions expire after an idle timeout and an absolute lifetime, can be revoked, and carry
a per-session CSRF token and an optional short-lived step-up (re-authentication) window."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from steward.config import Settings
from steward.models import AuthSession, User

COOKIE_NAME = "steward_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:  # SQLite drops tzinfo
        return dt.replace(tzinfo=timezone.utc)
    return dt


def create_session(db: Session, user: User, settings: Settings, ip: str) -> tuple[str, AuthSession]:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    sess = AuthSession(
        token_hash=_hash(token), user_id=user.id, csrf_token=secrets.token_urlsafe(24),
        created_at=now, last_seen_at=now, expires_at=now + timedelta(hours=settings.session_absolute_hours),
        ip=ip[:64],
    )
    db.add(sess)
    db.flush()
    return token, sess


def resolve(db: Session, token: str | None, settings: Settings) -> AuthSession | None:
    if not token or len(token) > 128:
        return None
    sess = db.execute(select(AuthSession).where(AuthSession.token_hash == _hash(token))).scalar_one_or_none()
    if sess is None or sess.revoked:
        return None
    now = datetime.now(timezone.utc)
    if now >= _aware(sess.expires_at):
        return None
    if now - _aware(sess.last_seen_at) > timedelta(minutes=settings.session_idle_minutes):
        return None
    sess.last_seen_at = now
    return sess


def revoke(db: Session, sess: AuthSession) -> None:
    sess.revoked = True


def revoke_all(db: Session, user_id: int) -> None:
    db.execute(update(AuthSession).where(AuthSession.user_id == user_id).values(revoked=True))


def csrf_ok(sess: AuthSession, header_value: str | None) -> bool:
    return bool(header_value) and hmac.compare_digest(sess.csrf_token, header_value)


def grant_step_up(sess: AuthSession, settings: Settings) -> datetime:
    sess.step_up_until = datetime.now(timezone.utc) + timedelta(minutes=settings.step_up_minutes)
    return sess.step_up_until


def step_up_active(sess: AuthSession) -> bool:
    until = _aware(sess.step_up_until)
    return bool(until and datetime.now(timezone.utc) < until)
