"""Shared FastAPI dependencies and the template environment.

Kept separate from main.py so routers can import these without importing the
app, which would be a circular import.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import __version__
from app.security import current_user

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Every template shows the version in the footer; injecting it globally avoids
# each route having to remember to pass it.
templates.env.globals["app_version"] = __version__


def age(timestamp: float | None) -> str:
    """A unix timestamp as how long ago it was, for showing beside a result.

    Relative rather than absolute because the question a stored result raises
    is "is this current?", which "4 minutes ago" answers and a wall-clock time
    in the server's timezone does not.
    """
    if not timestamp:
        return "never"
    seconds = max(0.0, time.time() - timestamp)
    if seconds < 45:
        return "just now"
    for limit, divisor, unit in (
        (3600, 60, "minute"),
        (86400, 3600, "hour"),
        (2592000, 86400, "day"),
    ):
        if seconds < limit:
            value = int(seconds // divisor)
            return f"{value} {unit}{'' if value == 1 else 's'} ago"
    return "over a month ago"


templates.env.filters["age"] = age


class RedirectToLogin(Exception):
    """Raised by login_required; turned into a 302 by the exception handler.

    A dependency cannot return a response, so it signals with an exception and
    main.py registers the handler that renders it.
    """

    def __init__(self, next_url: str) -> None:
        self.next_url = next_url
        super().__init__(f"login required for {next_url}")


def login_required(request: Request) -> str:
    """Return the logged-in username, or send an anonymous caller to /login."""
    username = current_user(request)
    if username is None:
        raise RedirectToLogin(request.url.path)
    return username


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    """Redirect after a successful POST.

    303 is the default so the browser follows with GET; a plain 302 leaves the
    method up to the client.
    """
    return RedirectResponse(url=url, status_code=status_code)
