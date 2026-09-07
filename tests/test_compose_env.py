"""Compose must deliver an Argon2 hash to the container unmangled.

An Argon2 hash is full of `$` ($argon2id$v=19$m=65536,...), and Compose treats
`$` as variable interpolation in two independent places. Getting either wrong
sends the container a hash with pieces missing, it refuses to start, and the
symptom is a wall of "variable is not set" warnings — which is exactly what
happened once already.

These are static checks on compose.yaml rather than a docker run, so they work
in CI without a daemon.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "compose.yaml"


def _compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_env_file_uses_raw_format() -> None:
    """Without `format: raw`, Compose interpolates values out of the env file."""
    assert "format: raw" in _compose_text(), (
        "compose.yaml must load the env file with `format: raw`, or the $ "
        "characters in the Argon2 password hash are eaten as variables."
    )


def test_env_file_is_not_called_dot_env() -> None:
    """Compose auto-loads ./.env to interpolate compose.yaml itself.

    That happens regardless of what env_file says, so the config file has to be
    named something else or the warnings and the mangling come back.
    """
    text = _compose_text()
    assert re.search(r"path:\s*xcp-pulse\.env\b", text), (
        "the env file must be named xcp-pulse.env, not .env — Compose "
        "auto-loads ./.env as its own interpolation source."
    )
    assert not re.search(r"^\s*-\s*\.env\s*$", text, re.MULTILINE), (
        "compose.yaml must not reference a bare .env file."
    )


def test_example_env_file_is_committed() -> None:
    """The sample must survive; the filled-in copy is gitignored."""
    assert (REPO_ROOT / "xcp-pulse.env.example").exists()


def test_real_env_file_is_gitignored() -> None:
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert "xcp-pulse.env" in ignored, "xcp-pulse.env holds the password hash"
    assert ".env" in ignored, "an older .env must not become committable"
