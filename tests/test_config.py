"""Configuration validation — the app must not start misconfigured."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigError, load_settings


def _clear_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "XCP_PULSE_ADMIN_USER",
        "XCP_PULSE_ADMIN_PASSWORD_HASH",
        "XCP_PULSE_SECRET_KEY",
        "XCP_PULSE_HTTPS",
        "XCP_PULSE_ENABLE_HTTPS",
        "XCP_PULSE_SESSION_HOURS",
        "XCP_PULSE_LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XCP_PULSE_DATA_DIR", str(tmp_path))


def test_missing_password_hash_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch, tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    # The error has to tell Nick how to fix it, not just what is wrong.
    assert "app.hashpw" in str(excinfo.value)


def test_plaintext_password_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XCP_PULSE_ADMIN_PASSWORD_HASH", "hunter2")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "Argon2" in str(excinfo.value)


def test_secret_key_is_generated_and_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XCP_PULSE_ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$fake")

    first = load_settings()
    assert first.secret_key
    assert (tmp_path / "secret_key").exists()

    # A restart must reuse the stored key, or every restart logs everyone out.
    assert load_settings().secret_key == first.secret_key


def test_bad_integer_setting_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XCP_PULSE_ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$fake")
    monkeypatch.setenv("XCP_PULSE_SESSION_HOURS", "twelve")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert "XCP_PULSE_SESSION_HOURS" in str(excinfo.value)


def test_neither_https_flag_set_defaults_both_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XCP_PULSE_ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$fake")
    settings = load_settings()
    assert settings.enable_https is False
    assert settings.https_only is False


def test_enable_https_implies_the_secure_cookie_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Built-in HTTPS means nginx is terminating TLS right there, so XCP Pulse
    # always knows the browser is on HTTPS without being told separately.
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XCP_PULSE_ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$fake")
    monkeypatch.setenv("XCP_PULSE_ENABLE_HTTPS", "true")
    settings = load_settings()
    assert settings.enable_https is True
    assert settings.https_only is True


def test_xcp_pulse_https_still_works_independently_of_built_in_https(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An external reverse proxy the operator runs themselves — built-in HTTPS
    # is off, but the cookie still needs Secure, and only an explicit flag can
    # tell XCP Pulse that.
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XCP_PULSE_ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$fake")
    monkeypatch.setenv("XCP_PULSE_HTTPS", "true")
    settings = load_settings()
    assert settings.enable_https is False
    assert settings.https_only is True


def test_xcp_pulse_https_can_override_enable_https_explicitly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An explicit XCP_PULSE_HTTPS=false always wins, even with built-in HTTPS
    # on — covers a deployment terminating TLS in front of nginx itself,
    # where the browser is on HTTPS for a different reason than nginx.
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("XCP_PULSE_ADMIN_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$fake")
    monkeypatch.setenv("XCP_PULSE_ENABLE_HTTPS", "true")
    monkeypatch.setenv("XCP_PULSE_HTTPS", "false")
    settings = load_settings()
    assert settings.enable_https is True
    assert settings.https_only is False
