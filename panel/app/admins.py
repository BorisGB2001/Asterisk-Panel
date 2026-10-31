"""Panel administrators (table panel.admins): super administrators manage accounts, viewers only see reports.

A new account gets a temporary password; on the first sign-in it sets up TOTP and then must change the password.

CLI (on the server, as the panel user):
  python -m app.admins create <username> [super|viewer]   — new account, prints a temporary password
  python -m app.admins reset <username>                   — new temporary password + TOTP set up again
"""
from __future__ import annotations

import secrets
import sys

import pyotp

from . import auth

ROLES = ("super", "viewer")


def get(db, admin_id: int):
    return db.one("SELECT * FROM admins WHERE id = %s", (admin_id,))


def by_name(db, username: str):
    return db.one("SELECT * FROM admins WHERE username = %s", ((username or "").strip()[:64],))


def all_admins(db) -> list[dict]:
    return db.all("SELECT * FROM admins ORDER BY username")


def temp_password() -> str:
    return secrets.token_urlsafe(12)


def create(db, username: str, role: str = "viewer") -> str:
    username = (username or "").strip()
    if not username or len(username) > 64 or not all(ch.isalnum() or ch in "._-@" for ch in username):
        raise ValueError("bad username")
    if role not in ROLES:
        raise ValueError("bad role")
    pw = temp_password()
    db.run("INSERT INTO admins (username, password_hash, role, must_change) VALUES (%s, %s, %s, true)",
           (username, auth.hash_password(pw), role))
    return pw


def reset(db, admin_id: int) -> str:
    pw = temp_password()
    db.run("UPDATE admins SET password_hash = %s, totp_secret = NULL, totp_last_step = 0, must_change = true, "
           "session_ver = session_ver + 1 WHERE id = %s", (auth.hash_password(pw), admin_id))
    return pw


def set_password(db, admin_id: int, password: str) -> None:
    db.run("UPDATE admins SET password_hash = %s, must_change = false, session_ver = session_ver + 1 WHERE id = %s",
           (auth.hash_password(password), admin_id))


def check_password(row, password: str) -> bool:
    if row is None or row["disabled"]:
        auth.verify_password(password, auth.DUMMY_HASH)   # same time as for a real account
        return False
    return auth.verify_password(password, row["password_hash"])


def check_totp(db, row, code: str) -> bool:
    if not row or not row["totp_secret"]:
        return False
    step = auth.totp_step(row["totp_secret"], code, row["totp_last_step"])
    if step is None:
        return False
    # atomic: the same code cannot be used twice even by parallel requests
    return db.run("UPDATE admins SET totp_last_step = %s WHERE id = %s AND totp_last_step < %s",
                  (step, row["id"], step)) == 1


def new_totp_secret() -> str:
    return pyotp.random_base32()


def bind_totp(db, row, secret: str, code: str) -> bool:
    step = auth.totp_step(secret, code, 0)
    if step is None:
        return False
    db.run("UPDATE admins SET totp_secret = %s, totp_last_step = %s WHERE id = %s", (secret, step, row["id"]))
    return True


def provisioning_uri(username: str, secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name="Asterisk panel")


def logged_in(db, row, ip: str) -> None:
    db.run("UPDATE admins SET last_login_at = now(), last_login_ip = %s WHERE id = %s", (ip, row["id"]))


def _cli() -> None:
    from .config import settings
    from .db import DB

    args = sys.argv[1:]
    if not args or args[0] not in ("create", "reset") or len(args) < 2:
        sys.exit(__doc__)
    db = DB(settings.database_url, size=1)
    db.migrate()
    if args[0] == "create":
        pw = create(db, args[1], args[2] if len(args) > 2 else "super")
    else:
        row = by_name(db, args[1])
        if not row:
            sys.exit("no such user")
        pw = reset(db, row["id"])
    db.event("acct_" + args[0], {"username": args[1]}, actor="cli")
    print(f"user: {args[1]}\ntemporary password: {pw}\n"
          "Sign in with an empty code: the panel will offer to set up the authenticator, then to change the password.")


if __name__ == "__main__":
    _cli()
