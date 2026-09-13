"""Static assets are cached aggressively, keyed by a content-derived URL.

A stylesheet fix reaching the container is not the same as it reaching the
browser: without an explicit Cache-Control, a browser falls back to its own
heuristics for how long to trust a cached copy, and in practice that can
survive even a hard reload — a fixed file sits on disk, correct, while the
page keeps rendering the old one. See app/main.py:_CacheableStaticFiles and
app/dependencies.py:_asset_token, which this pair exists to close.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_the_stylesheet_is_served_with_a_long_lived_immutable_cache(
    logged_in: TestClient,
) -> None:
    response = logged_in.get("/static/style.css")
    assert response.status_code == 200
    cache_control = response.headers["cache-control"]
    assert "immutable" in cache_control
    assert "max-age=31536000" in cache_control
