"""Tamper-evident audit log.

Each user has their own hash chain:  hash_n = SHA-256(canonical_json(entry_n + hash_{n-1})).
Editing, deleting or reordering any entry breaks every later hash, which `verify_chain`
detects. Per-user chains keep appends contention-free and let the table shard by user.
For production, periodically anchor each chain head in write-once storage (e.g. S3 Object
Lock) so an attacker with database access cannot rewrite a whole chain undetected.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from steward.models import AuditEntry

GENESIS = "0" * 64
SENSITIVE_KEYS = {"password", "token", "csrf", "secret", "api_key", "authorization", "cookie"}


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def _scrub(payload: dict) -> dict:
    clean = {}
    for k, v in payload.items():
        if any(s in k.lower() for s in SENSITIVE_KEYS):
            clean[k] = "[redacted]"
        elif isinstance(v, dict):
            clean[k] = _scrub(v)
        else:
            clean[k] = v
    return clean


def entry_hash(user_id: int, seq: int, ts: str, actor: str, event: str, payload: dict, prev_hash: str) -> str:
    body = canonical({"user_id": user_id, "seq": seq, "ts": ts, "actor": actor, "event": event,
                      "payload": payload, "prev_hash": prev_hash})
    return hashlib.sha256(body.encode()).hexdigest()


def lock_user(db: Session, user_id: int) -> None:
    """On PostgreSQL, take the user's write lock until this transaction commits or rolls back.

    It is a transaction-scoped advisory lock, so it spans worker processes and threads, and the
    same transaction can take it again without blocking. Taking it before reading anything a
    write depends on (balances, the sandbox date, the chain head) serialises one user's writes;
    other users never wait. SQLite (development only) has a single writer and needs nothing here."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 0x5743_0000 + user_id})


def append(db: Session, user_id: int, actor: str, event: str, payload: dict | None = None) -> AuditEntry:
    """Append an entry to the user's chain. Caller owns the transaction (commit).

    Appends for one user are serialised by `lock_user`, held until the transaction ends, so the
    next append reads the committed chain head. The UNIQUE(user_id, seq) constraint is the
    backstop that makes a lost race fail loudly instead of forking the chain. (An in-process
    lock released before commit would add nothing and, combined with the advisory lock, can
    deadlock two requests of the same user.)"""
    payload = json.loads(canonical(_scrub(payload or {})))
    lock_user(db, user_id)
    last = db.execute(
        select(AuditEntry).where(AuditEntry.user_id == user_id).order_by(AuditEntry.seq.desc()).limit(1)
    ).scalar_one_or_none()
    seq = (last.seq + 1) if last else 1
    prev = last.hash if last else GENESIS
    ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    entry = AuditEntry(
        user_id=user_id, seq=seq, ts=ts, actor=actor, event=event, payload=payload, prev_hash=prev,
        hash=entry_hash(user_id, seq, ts, actor, event, payload, prev),
    )
    db.add(entry)
    db.flush()
    return entry


def verify_chain(db: Session, user_id: int) -> dict:
    entries = db.execute(
        select(AuditEntry).where(AuditEntry.user_id == user_id).order_by(AuditEntry.seq)
    ).scalars().all()
    prev = GENESIS
    for i, e in enumerate(entries, start=1):
        problem = None
        if e.seq != i:
            problem = f"sequence gap: expected {i}, found {e.seq}"
        elif e.prev_hash != prev:
            problem = "prev_hash does not match the previous entry"
        elif e.hash != entry_hash(e.user_id, e.seq, e.ts, e.actor, e.event, e.payload, e.prev_hash):
            problem = "entry contents do not match its hash"
        if problem:
            return {"ok": False, "entries": len(entries), "first_bad_seq": e.seq, "problem": problem,
                    "head_hash": entries[-1].hash if entries else GENESIS}
        prev = e.hash
    return {"ok": True, "entries": len(entries), "first_bad_seq": None, "problem": None, "head_hash": prev}
