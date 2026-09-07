"""Liveness endpoint.

Deliberately unauthenticated: the compose healthcheck has no session, and this
reveals nothing beyond the fact that the app is running.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
