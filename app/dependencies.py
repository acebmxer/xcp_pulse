"""Shared FastAPI dependencies and the template environment.

Kept separate from main.py so routers can import these without importing the
app, which would be a circular import.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import __version__
from app.artifacts import artifact_path, get_artifact
from app.security import current_user

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Every template shows the version in the footer; injecting it globally avoids
# each route having to remember to pass it.
templates.env.globals["app_version"] = __version__


def _asset_token() -> str:
    """A cache-busting token for the stylesheet, from its own mtime.

    Starlette's StaticFiles sends an ETag and Last-Modified but no Cache-Control,
    so a browser is free to reuse a cached stylesheet without revalidating it.
    In practice it does: a CSS change would reach the container and still not
    reach the page, which is invisible from the server side and looks exactly
    like a fix that did not work.

    The version string alone is not enough, because it does not move between
    builds during development — which is precisely when the stylesheet changes
    most. The mtime does.
    """
    try:
        return str(int((STATIC_DIR / "style.css").stat().st_mtime))
    except OSError:
        return __version__


templates.env.globals["asset_token"] = _asset_token()


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


def count(value: int) -> str:
    """A hit count as something to read at a glance.

    Thousands separators, because a redaction report puts rule counts in one
    column and their range is enormous: session tokens run to six figures on a
    two-host pool — the toolstack logs a `trackid` every time it authenticates
    to itself — while email addresses run to a dozen. Unseparated, 464679 and
    12 are the same shape at a glance, and the small counts are the ones an
    operator is actually checking before sending a bundle out.
    """
    return f"{value:,}"


def counts_in(text: str | None) -> str:
    """Thousands-separate the bare integers in a stored progress line.

    A job's step text is written into the database when the job runs, so
    formatting it at write time leaves every row recorded before that change
    unseparated for good — and those rows are most of what the page shows.
    Separating here instead means one code path, applied on every render, and
    the history reads consistently regardless of which version wrote it.

    Only runs of four or more digits are touched, and only whole ones: a byte
    size ("858.0 MiB") and a duration keep their own formatting because the
    digits either side of a dot are not a standalone integer. A digit run
    glued to a letter on either side — a log bundle member's name, scanned
    into this same step text verbatim (e.g. "Scanning var/log/sa/sa20250911")
    — is left alone too, since it is a filename, not a count.
    """
    if not text:
        return ""
    return re.sub(
        r"(?<![\d.\w])\d{4,}(?![\d.\w])",
        lambda m: f"{int(m.group(0)):,}",
        text,
    )


def date_coverage(report, fallback: str = "") -> str:
    """How to phrase the window a findings report covers, for the page header.

    Delegates to ``Report.coverage_text`` so the page and the downloaded
    Markdown copy (``job_findings.to_markdown``) can never disagree about
    what a run covered. ``fallback`` covers a page rendered with no report at
    all yet.
    """
    return report.coverage_text if report is not None else fallback


templates.env.filters["age"] = age
templates.env.filters["count"] = count
templates.env.filters["counts_in"] = counts_in
templates.env.filters["date_coverage"] = date_coverage


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


def wake_worker(request: Request) -> None:
    """Tell the job worker to look now rather than at its next poll.

    Here rather than in one router because every route that enqueues a job
    needs it, and a second copy would be a route that queues work the operator
    then watches sit still for a second. Absent when jobs run in a separate
    process — the queue is the database either way — so its absence is not an
    error.
    """
    worker = getattr(request.app.state, "job_worker", None)
    if worker is not None:
        worker.wake()


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    """Redirect after a successful POST.

    303 is the default so the browser follows with GET; a plain 302 leaves the
    method up to the client.
    """
    return RedirectResponse(url=url, status_code=status_code)


def serve_artifact(request: Request, artifact_id: str, *, on_error: str) -> Response:
    """Stream one stored artifact to the browser as a download.

    Shared by every page that lists artifacts, rather than one copy per router:
    the collect page and the jobs page offer the same files from the same store,
    and two implementations would mean a fix to one leaving the other wrong.

    ``FileResponse`` streams from disk, so a 433 MB bundle is never held in
    memory. The name the browser saves under comes from the artifact row, not
    from the path — the file on disk is a uuid, which is what stops a name from
    Xen Orchestra reaching the filesystem at all.

    ``on_error`` is the page to send the operator back to, so the message lands
    where they pressed the button.
    """
    db = request.app.state.db
    data_dir = request.app.state.settings.data_dir

    artifact = get_artifact(db, artifact_id)
    if artifact is None:
        return redirect(f"{on_error}?error=That+file+is+no+longer+stored.")

    path = artifact_path(data_dir, artifact.job_id, artifact.id)
    if not path.is_file():
        return redirect(f"{on_error}?error=That+file+is+missing+from+the+data+volume.")

    return FileResponse(path, media_type=artifact.media_type, filename=artifact.name)
