"""Configuration, read once from the environment at import time.

This is the ONLY module that reads os.environ. Everything else imports
``settings`` from here, so there is exactly one place to look for what a
setting is called, what it defaults to, and what validates it.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """A setting is missing or unusable, and the app must not start."""


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Everything the app needs to run, resolved and validated."""

    data_dir: Path
    admin_user: str
    admin_password_hash: str
    secret_key: str
    https_only: bool
    enable_https: bool
    session_hours: int
    login_max_attempts: int
    login_lockout_minutes: int
    log_level: str
    enable_self_update: bool
    compose_project_dir: str
    is_dev_build: bool

    # Derived paths. Kept here so no other module builds a path by hand.
    db_path: Path = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", self.data_dir / "xcp-pulse.db")


def _load_or_create_secret_key(data_dir: Path) -> str:
    """Return the session signing key, generating one on first boot.

    Preferring an explicit XCP_PULSE_SECRET_KEY lets the operator control it.
    Falling back to a generated key persisted on the data volume means sessions
    survive a restart without the secret ever being baked into the image.
    """
    explicit = _env_str("XCP_PULSE_SECRET_KEY")
    if explicit:
        return explicit

    key_file = data_dir / "secret_key"
    if key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored

    generated = secrets.token_urlsafe(48)
    key_file.write_text(generated, encoding="utf-8")
    # The key signs session cookies; it will also derive the key that encrypts
    # the stored XO token. Owner-only, always.
    key_file.chmod(0o600)
    return generated


def load_settings() -> Settings:
    """Build the settings object, or raise ConfigError with a fix to apply."""
    data_dir = Path(_env_str("XCP_PULSE_DATA_DIR", "/data"))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create data directory {data_dir}: {exc}") from exc

    password_hash = _env_str("XCP_PULSE_ADMIN_PASSWORD_HASH")
    if not password_hash:
        raise ConfigError(
            "XCP_PULSE_ADMIN_PASSWORD_HASH is not set, so there is no way to log in.\n"
            "\n"
            "Generate one with:\n"
            "    docker compose run --rm xcp-pulse python -m app.hashpw\n"
            "\n"
            "then paste the printed hash into your .env file as\n"
            "    XCP_PULSE_ADMIN_PASSWORD_HASH=$argon2id$...\n"
            "\n"
            "XCP Pulse will not start with a default password."
        )
    if not password_hash.startswith("$argon2"):
        raise ConfigError(
            "XCP_PULSE_ADMIN_PASSWORD_HASH does not look like an Argon2 hash "
            "(it should begin with '$argon2id$'). It must be a hash, not a "
            "plaintext password. Generate one with: python -m app.hashpw"
        )

    # Built-in HTTPS (docker/entrypoint.sh's nginx) means XCP Pulse always
    # knows the browser is on HTTPS — nginx is terminating it right there —
    # so enabling it implies the Secure cookie flag without being asked
    # separately. XCP_PULSE_HTTPS remains for the other case: an external
    # reverse proxy the operator runs themselves, which this setting cannot
    # detect on its own.
    enable_https = _env_bool("XCP_PULSE_ENABLE_HTTPS", False)

    return Settings(
        data_dir=data_dir,
        admin_user=_env_str("XCP_PULSE_ADMIN_USER", "admin"),
        admin_password_hash=password_hash,
        secret_key=_load_or_create_secret_key(data_dir),
        https_only=_env_bool("XCP_PULSE_HTTPS", enable_https),
        enable_https=enable_https,
        session_hours=_env_int("XCP_PULSE_SESSION_HOURS", 12),
        login_max_attempts=_env_int("XCP_PULSE_LOGIN_MAX_ATTEMPTS", 5),
        login_lockout_minutes=_env_int("XCP_PULSE_LOGIN_LOCKOUT_MINUTES", 15),
        log_level=_env_str("XCP_PULSE_LOG_LEVEL", "INFO").upper(),
        # Self-update: off unless the operator has deliberately mounted the
        # Docker socket and set the project directory below — see
        # app/update.py's module docstring for why this is opt-in rather than
        # always available.
        enable_self_update=_env_bool("XCP_PULSE_ENABLE_SELF_UPDATE", False),
        compose_project_dir=_env_str("XCP_PULSE_COMPOSE_PROJECT_DIR"),
        # Baked in by the Dockerfile's XCP_PULSE_DEV_BUILD ARG, which only
        # docker-compose.dev.yml ever sets — see its comment, and
        # app/update.py's use of this flag.
        is_dev_build=_env_bool("XCP_PULSE_DEV_BUILD", False),
    )
