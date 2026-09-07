"""Generate an Argon2id hash for XCP_PULSE_ADMIN_PASSWORD_HASH.

Run it as:

    docker compose run --rm xcp-pulse python -m app.hashpw

The password is read from a prompt rather than an argument so it does not end
up in shell history or in the process list.
"""

from __future__ import annotations

import getpass
import sys

from app.security import hash_password


def main() -> int:
    if sys.stdin.isatty():
        password = getpass.getpass("Password for the XCP Pulse admin user: ")
        confirm = getpass.getpass("Repeat the password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1
    else:
        # Allows: echo 'secret' | python -m app.hashpw
        password = sys.stdin.readline().rstrip("\n")

    if not password:
        print("Password must not be empty.", file=sys.stderr)
        return 1
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        return 1

    print()
    print("Add this line to your .env file:")
    print()
    print(f"XCP_PULSE_ADMIN_PASSWORD_HASH={hash_password(password)}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
