"""The health endpoint must work without a session — compose depends on it."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import __version__


def test_healthz_is_reachable_without_logging_in(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_api_docs_are_not_exposed(client: TestClient) -> None:
    # This app stands in front of credentials; the unauthenticated FastAPI docs
    # stay off until there is a reason to expose them.
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, f"{path} should not be served"
