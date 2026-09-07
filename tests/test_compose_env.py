"""Compose must deliver an Argon2 hash to the container unmangled.

An Argon2 hash is full of `$` ($argon2id$v=19$m=65536,...), and Compose treats
`$` as variable interpolation in two independent places. Getting either wrong
sends the container a hash with pieces missing, it refuses to start, and the
symptom is a wall of "variable is not set" warnings — which is exactly what
happened once already.

These are static checks on docker-compose.yml.example rather than a docker
run, so they work in CI without a daemon. The sample is what a clone gets and
what an operator copies, so it is the file that has to be right.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml.example"


def _compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def test_env_file_uses_raw_format() -> None:
    """Without `format: raw`, Compose interpolates values out of the env file."""
    assert "format: raw" in _compose_text(), (
        "docker-compose.yml must load the env file with `format: raw`, or the $ "
        "characters in the Argon2 password hash are eaten as variables."
    )


def test_env_file_is_not_called_dot_env() -> None:
    """Compose auto-loads ./.env to interpolate docker-compose.yml itself.

    That happens regardless of what env_file says, so the config file has to be
    named something else or the warnings and the mangling come back.
    """
    text = _compose_text()
    assert re.search(r"path:\s*xcp-pulse\.env\b", text), (
        "the env file must be named xcp-pulse.env, not .env — Compose "
        "auto-loads ./.env as its own interpolation source."
    )
    assert not re.search(r"^\s*-\s*\.env\s*$", text, re.MULTILINE), (
        "docker-compose.yml must not reference a bare .env file."
    )


def test_example_files_are_committed() -> None:
    """The samples must survive; the filled-in copies are gitignored."""
    assert (REPO_ROOT / "xcp-pulse.env.example").exists()
    assert (REPO_ROOT / "docker-compose.yml.example").exists()


def test_working_files_are_gitignored() -> None:
    """A deployment's own files must never become committable.

    Both are per-deployment: the env file holds the password hash and the
    encryption key, and docker-compose.yml carries whatever the operator
    changed — image tag, port binding, local overrides. Only the samples are
    tracked.
    """
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert "xcp-pulse.env" in ignored, "xcp-pulse.env holds the password hash"
    assert ".env" in ignored, "an older .env must not become committable"
    assert "docker-compose.yml" in ignored, "docker-compose.yml is a deployment's own file"


def test_dev_override_only_adds_a_build() -> None:
    """The dev overlay must stay an overlay, not a second deployment file.

    It is layered on top of docker-compose.yml, so anything it declares beyond
    `build:` silently overrides the real deployment's setting — an image tag or
    port here would apply to local builds only and diverge without warning.
    """
    dev = REPO_ROOT / "docker-compose.dev.yml"
    assert dev.exists(), "docker-compose.dev.yml is how a clone builds the image"

    # Comments explain the overlay and mention these keys by name, so match
    # real YAML lines rather than the raw text.
    settings = re.findall(r"^\s+([a-z_]+):", dev.read_text(encoding="utf-8"), re.M)
    assert "build" in settings, "the overlay exists to add `build: .`"
    for key in ("image", "ports", "env_file", "volumes"):
        assert key not in settings, (
            f"{key} belongs in docker-compose.yml, not the dev overlay — "
            "declaring it here overrides the deployment for local builds only."
        )
