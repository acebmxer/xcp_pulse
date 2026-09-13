"""User accounts and roles.

Before this module existed, "who can log in" was a single username/password
hash pair read from the environment. That pair (``XCP_PULSE_ADMIN_USER`` /
``XCP_PULSE_ADMIN_PASSWORD_HASH``) still works, but only as the seed for the
first admin account — see ``bootstrap_admin`` — so an existing deployment's
``.env`` keeps working unchanged after upgrading.

Three roles, enforced both here (the CHECK constraint in the users table) and
in app/dependencies.py (the actual route guards):

- ``admin``    — everything: user management, XO connection, redaction rules,
                 plus everything operator can do.
- ``operator`` — run and download collections/redactions/extractions/support
                 packages, delete artifacts, view the activity log. Cannot
                 touch XO connection settings, redaction rules, or users.
- ``viewer``   — read-only: dashboard, findings, jobs, activity log. No
                 buttons that change anything.

Every role can change their own password; only admin can reset someone
else's — see ``set_password`` vs the admin-only reset path in
app/routes/users.py.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass

from app.security import hash_password, verify_password

ROLES = ("admin", "operator", "viewer")

# A real Argon2 hash of an unguessable, unused password. authenticate() verifies
# against this when the username doesn't exist, so a login attempt costs the
# same either way and a caller cannot tell "wrong password" from "no such
# user" by timing. Computed once at import time rather than per call, since
# Argon2 is deliberately slow.
_DUMMY_HASH = hash_password(str(uuid.uuid4()))


class UserError(ValueError):
    """A user operation was rejected — bad role, duplicate username, etc."""


MIN_PASSWORD_LENGTH = 8


@dataclass(frozen=True)
class User:
    id: str
    username: str
    password_hash: str
    role: str
    disabled: bool
    created_at: float


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        password_hash=row["password_hash"],
        role=row["role"],
        disabled=bool(row["disabled"]),
        created_at=row["created_at"],
    )


def bootstrap_admin(conn: sqlite3.Connection, admin_user: str, admin_password_hash: str) -> None:
    """Create the first admin account from the environment, if none exist yet.

    Runs on every startup but only acts once: once any row exists in
    ``users``, the environment variables are no longer consulted. This is what
    lets XCP_PULSE_ADMIN_USER / XCP_PULSE_ADMIN_PASSWORD_HASH keep working as
    "the initial account" without also making them the only account, or
    letting a stale .env value silently reset the admin's real password on
    every restart.
    """
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    if row["n"] > 0:
        return
    conn.execute(
        "INSERT INTO users (id, username, password_hash, role, disabled, created_at) "
        "VALUES (?, ?, ?, 'admin', 0, ?)",
        (str(uuid.uuid4()), admin_user, admin_password_hash, time.time()),
    )
    conn.commit()


def get_user(conn: sqlite3.Connection, username: str) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _row_to_user(row) if row is not None else None


def get_user_by_id(conn: sqlite3.Connection, user_id: str) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_user(row) if row is not None else None


def list_users(conn: sqlite3.Connection) -> list[User]:
    rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
    return [_row_to_user(row) for row in rows]


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str) -> User:
    username = username.strip()
    if not username:
        raise UserError("Username cannot be blank.")
    if role not in ROLES:
        raise UserError(f"Role must be one of {', '.join(ROLES)}.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if get_user(conn, username) is not None:
        raise UserError(f"A user named {username!r} already exists.")

    user = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=hash_password(password),
        role=role,
        disabled=False,
        created_at=time.time(),
    )
    conn.execute(
        "INSERT INTO users (id, username, password_hash, role, disabled, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?)",
        (user.id, user.username, user.password_hash, user.role, user.created_at),
    )
    conn.commit()
    return user


def set_role(conn: sqlite3.Connection, user_id: str, role: str) -> None:
    if role not in ROLES:
        raise UserError(f"Role must be one of {', '.join(ROLES)}.")
    _guard_not_last_admin(conn, user_id, changing_role_to=role)
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()


def set_disabled(conn: sqlite3.Connection, user_id: str, disabled: bool) -> None:
    if disabled:
        _guard_not_last_admin(conn, user_id, disabling=True)
    conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id))
    conn.commit()


def set_password(conn: sqlite3.Connection, user_id: str, new_password: str) -> None:
    """Set a user's password directly — used both for "change my own password"
    and for an admin's reset of someone else's. The caller (app/routes/users.py)
    is what decides whether the current request is allowed to call this for the
    given ``user_id``; this function itself does not check who is asking.
    """
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), user_id),
    )
    conn.commit()


class AccountDisabled(Exception):
    """The password was correct, but the account is disabled.

    Raised only when the password actually matches — a wrong password against
    a disabled account still fails as "incorrect username or password", the
    same as any other wrong password, so someone without the real password
    learns nothing about whether the account exists or its state. Only
    someone who already knows the correct password learns it's disabled.
    """


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> User | None:
    """Return the user if the password is correct and the account is usable.

    Verifies the password even when the account is missing — a dummy hash
    keeps timing consistent with the real path so a caller cannot tell "wrong
    password" from "no such user" by response time.

    Raises AccountDisabled instead of returning None when the account is
    disabled but the password was right, so the login route can show that
    specifically rather than the generic wrong-credentials message.
    """
    user = get_user(conn, username)
    password_hash = user.password_hash if user is not None else _DUMMY_HASH
    password_ok = verify_password(password, password_hash)
    if user is None or not password_ok:
        return None
    if user.disabled:
        raise AccountDisabled()
    return user


def _guard_not_last_admin(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    disabling: bool = False,
    changing_role_to: str | None = None,
) -> None:
    """Refuse to leave the app with zero usable admins.

    Without this, disabling or demoting the only admin locks every account
    management screen behind a role nothing can satisfy, with no way back in
    short of editing the database by hand.
    """
    target = get_user_by_id(conn, user_id)
    if target is None or target.role != "admin" or target.disabled:
        return  # not currently an active admin, so this can't remove the last one
    if disabling or changing_role_to != "admin":
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0 AND id != ?",
            (user_id,),
        ).fetchone()
        if row["n"] == 0:
            raise UserError("Cannot remove the last active admin account.")
