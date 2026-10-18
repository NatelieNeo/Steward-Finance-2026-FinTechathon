"""Runtime configuration, read once from the environment (and an optional .env file)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, no interpolation. Never overrides real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    cookie_secure: bool
    session_idle_minutes: int
    session_absolute_hours: int
    step_up_minutes: int
    llm_provider: str  # "auto" | "anthropic" | "offline"
    llm_model: str
    llm_effort: str
    allow_registration: bool
    demo_seed: int
    currency: str
    env: str

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(ROOT_DIR / ".env")
    default_db = f"sqlite:///{(ROOT_DIR / 'data' / 'steward.db').as_posix()}"
    return Settings(
        database_url=os.environ.get("DATABASE_URL", default_db),
        cookie_secure=_bool("COOKIE_SECURE", False),
        session_idle_minutes=int(os.environ.get("SESSION_IDLE_MINUTES", "30")),
        session_absolute_hours=int(os.environ.get("SESSION_ABSOLUTE_HOURS", "12")),
        step_up_minutes=int(os.environ.get("STEP_UP_MINUTES", "5")),
        llm_provider=os.environ.get("STEWARD_LLM_PROVIDER", "auto").lower(),
        llm_model=os.environ.get("STEWARD_LLM_MODEL", "claude-opus-5-5"),
        llm_effort=os.environ.get("STEWARD_LLM_EFFORT", "low"),
        allow_registration=_bool("ALLOW_REGISTRATION", True),
        demo_seed=int(os.environ.get("DEMO_SEED", "42")),
        currency=os.environ.get("STEWARD_CURRENCY", "SGD"),
        env=os.environ.get("STEWARD_ENV", "development").lower(),
    )


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
