"""Application entry point: builds the FastAPI app and wires everything up."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.config import Settings, load_settings
from app.db import init_db
from app.dependencies import STATIC_DIR, RedirectToLogin

# Importing a job module is what registers its kind with the runner, which
# deliberately holds no list of its own. Anything defining a job kind has to be
# imported here or its jobs fail at run time with "no handler".
from app.job_collect import KIND as _COLLECT_KIND  # noqa: F401
from app.job_extract import KIND as _EXTRACT_KIND  # noqa: F401
from app.job_findings import KIND as _FINDINGS_KIND  # noqa: F401
from app.job_inventory import KIND as _INVENTORY_KIND  # noqa: F401
from app.job_log_findings import KIND as _LOG_FINDINGS_KIND  # noqa: F401
from app.job_redact import KIND as _REDACT_KIND  # noqa: F401
from app.job_runner import JobWorker
from app.job_support_package import KIND as _SUPPORT_PACKAGE_KIND  # noqa: F401
from app.jobs import reset_orphans
from app.logging_conf import configure_logging
from app.routes import auth, collect, dashboard, docs, health, redaction
from app.routes import findings as findings_routes
from app.routes import jobs as job_routes
from app.routes import settings as settings_routes
from app.routes import support_package as support_package_routes
from app.security import purge_expired_sessions, purge_old_login_attempts


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    log = configure_logging(settings.log_level)

    app.state.db = init_db(settings.db_path)

    # Rows from a previous run are worthless: expired sessions cannot be used
    # and stale failures would keep an address locked out past its window.
    sessions = purge_expired_sessions(app.state.db)
    attempts = purge_old_login_attempts(app.state.db, settings.login_lockout_minutes)

    # A job recorded as running belongs to a thread that died with the previous
    # process. Marking those failed at startup is the only moment it can be done
    # safely, because nothing can legitimately be running yet.
    orphans = reset_orphans(app.state.db)
    if orphans:
        log.warning("marked %d interrupted job(s) as failed", orphans)

    app.state.job_worker = JobWorker(settings.db_path, settings.data_dir, settings)
    app.state.job_worker.start()

    log.info(
        "XCP Pulse %s started (data=%s, purged %d sessions, %d login attempts)",
        __version__,
        settings.data_dir,
        sessions,
        attempts,
    )

    yield

    app.state.job_worker.stop()
    app.state.db.close()
    log.info("XCP Pulse stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Tests pass their own settings; production loads the env."""
    app = FastAPI(
        title="XCP Pulse",
        description="Collects XCP-ng and Xen Orchestra logs, bundles them, and reports findings.",
        version=__version__,
        lifespan=lifespan,
        # The docs are unauthenticated in FastAPI, and this app stands in front
        # of credentials, so they stay off until there is a reason to expose them.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings or load_settings()

    @app.exception_handler(RedirectToLogin)
    async def _redirect_to_login(request: Request, exc: RedirectToLogin) -> RedirectResponse:
        return RedirectResponse(url=f"/login?next={exc.next_url}", status_code=303)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(settings_routes.router)
    app.include_router(job_routes.router)
    app.include_router(redaction.router)
    app.include_router(collect.router)
    app.include_router(findings_routes.router)
    app.include_router(support_package_routes.router)
    app.include_router(docs.router)
    return app


def __getattr__(name: str) -> FastAPI:
    """Build the production app only when `app` is actually asked for.

    uvicorn imports `app.main:app`, which must construct the app. But building
    it at import time would read the environment and create the data directory
    as a side effect of merely importing this module — which breaks tests and
    any tooling that imports it. PEP 562 lets the import do the work only when
    the attribute is reached.
    """
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
