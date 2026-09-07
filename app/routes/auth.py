"""Login and logout."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.dependencies import redirect, templates
from app.security import (
    SESSION_COOKIE,
    clear_login_failures,
    client_ip,
    create_session,
    current_user,
    destroy_session,
    is_rate_limited,
    record_login_failure,
    sign_session_id,
    unsign_session_id,
    verify_password,
)

router = APIRouter()


def _set_session_cookie(response: Response, request: Request, session_id: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        key=SESSION_COOKIE,
        value=sign_session_id(session_id, settings.secret_key),
        max_age=settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.https_only,
        path="/",
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> Response:
    # Already signed in — no reason to show the form again.
    if current_user(request) is not None:
        return redirect(next or "/")
    return templates.TemplateResponse(request, "login.html", {"next_url": next})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> Response:
    settings = request.app.state.settings
    conn = request.app.state.db
    ip = client_ip(request)

    if is_rate_limited(conn, ip, settings.login_max_attempts, settings.login_lockout_minutes):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": (
                    f"Too many failed attempts. Try again in "
                    f"{settings.login_lockout_minutes} minutes."
                ),
                "next_url": next,
            },
            status_code=429,
        )

    # Verify the password even when the username is wrong, so a bad username
    # and a bad password take the same time and cannot be told apart.
    password_ok = verify_password(password, settings.admin_password_hash)
    if username != settings.admin_user or not password_ok:
        record_login_failure(conn, ip)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect username or password.", "next_url": next},
            status_code=401,
        )

    clear_login_failures(conn, ip)
    session_id = create_session(conn, username, settings.session_hours)

    # Only ever redirect within this site: an attacker-supplied ?next=https://…
    # would otherwise turn the login page into an open redirect.
    target = next if next.startswith("/") and not next.startswith("//") else "/"
    response = redirect(target)
    _set_session_cookie(response, request, session_id)
    return response


@router.post("/logout")
def logout(request: Request) -> Response:
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        session_id = unsign_session_id(cookie, request.app.state.settings.secret_key)
        if session_id:
            destroy_session(request.app.state.db, session_id)

    response = redirect("/login")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response
