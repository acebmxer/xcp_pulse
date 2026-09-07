"""Shared test fixtures.

Every test gets its own temporary data directory and database, so tests never
share state and never touch a real deployment.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import hash_password

TEST_USER = "admin"
TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        admin_user=TEST_USER,
        admin_password_hash=hash_password(TEST_PASSWORD),
        secret_key="test-secret-key-not-for-production",
        https_only=False,
        session_hours=12,
        login_max_attempts=5,
        login_lockout_minutes=15,
        log_level="WARNING",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """A test client that does not follow redirects.

    Redirects are part of what these tests assert (anonymous callers are sent
    to /login), so following them silently would hide the behaviour.
    """
    app = create_app(settings)
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def logged_in(client: TestClient) -> TestClient:
    response = client.post(
        "/login",
        data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/"},
    )
    assert response.status_code == 303, "fixture could not log in"
    return client
