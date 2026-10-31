"""Personal cabinet sign-in for subscribers: extension (or e-mail) + cabinet password + TOTP (mandatory).

The cabinet password is not the SIP password. An administrator gives access with a temporary password;
on the first sign-in the subscriber sets up the authenticator and then a new password, as administrators do.
"""
from __future__ import annotations

import secrets

from . import auth


def by_login(db, login: str):
    login = (login or "").strip()[:120]
    if not login:
        return None
    if "@" in login:
        return db.one("SELECT * FROM subscribers WHERE lower(email) = lower(%s)", (login,))
    return db.one("SELECT * FROM subscribers WHERE ext = %s", (login,))


def get(db, ext: str):
    return db.one("SELECT * FROM subscribers WHERE ext = %s", (ext,))


def check_password(row, password: str) -> bool:
    if row is None or not row["web_enabled"] or not row["password_hash"]:
        auth.verify_password(password, auth.DUMMY_HASH)
        return False
    return auth.verify_password(password, row["password_hash"])


def check_totp(db, row, code: str) -> bool:
    if not row or not row["totp_secret"]:
        return False
    step = auth.totp_step(row["totp_secret"], code, row["totp_last_step"])
    if step is None:
        return False
    return db.run("UPDATE subscribers SET totp_last_step = %s WHERE ext = %s AND totp_last_step < %s",
                  (step, row["ext"], step)) == 1


def bind_totp(db, row, secret: str, code: str) -> bool:
    step = auth.totp_step(secret, code, 0)
    if step is None:
        return False
    db.run("UPDATE subscribers SET totp_secret = %s, totp_last_step = %s WHERE ext = %s", (secret, step, row["ext"]))
    return True


def grant(db, ext: str) -> str:
    """Opens the cabinet (or resets access): temporary password, authenticator set up again."""
    pw = secrets.token_urlsafe(12)
    db.run("UPDATE subscribers SET web_enabled = true, password_hash = %s, totp_secret = NULL, totp_last_step = 0, "
           "must_change = true, session_ver = session_ver + 1 WHERE ext = %s", (auth.hash_password(pw), ext))
    return pw


def revoke(db, ext: str) -> None:
    db.run("UPDATE subscribers SET web_enabled = false, session_ver = session_ver + 1 WHERE ext = %s", (ext,))


def set_password(db, ext: str, password: str) -> None:
    db.run("UPDATE subscribers SET password_hash = %s, must_change = false, session_ver = session_ver + 1 WHERE ext = %s",
           (auth.hash_password(password), ext))


def logged_in(db, row, ip: str) -> None:
    db.run("UPDATE subscribers SET last_login_at = now(), last_login_ip = %s WHERE ext = %s", (ip, row["ext"]))
