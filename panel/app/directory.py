"""Subscribers and groups (tables panel.subscribers, panel.groups, panel.group_members).

The panel is the source of truth: provision.py turns these tables into Asterisk config files.
Group <id> is Asterisk queue q<id> and dialplan context queue-<id>; the IVR jumps to queue-<id>.
"""
from __future__ import annotations

import hashlib
import re
import secrets

from .fmt import N_
from .options import EMAIL_RE, STRATEGIES

EXT_RE = re.compile(r"^[0-9]{2,8}$")
TG_USER_RE = re.compile(r"^@?[A-Za-z0-9_]{4,32}$")
REALM = "asterisk"


class Invalid(ValueError):
    """Message for the user (an English phrase from the locale catalogs)."""


def clean_name(v: str, limit: int = 60) -> str:
    """Names go into callerid="..." and comments of config files: no quotes, brackets, separators or control chars."""
    v = re.sub(r"[\x00-\x1f\x7f]", " ", v or "")
    v = re.sub(r"[\"<>;\\\[\]]", "", v)
    return " ".join(v.split())[:limit]


def clean_email(v: str) -> str | None:
    v = (v or "").strip()
    if not v:
        return None
    if not EMAIL_RE.match(v):
        raise Invalid(N_("Invalid e-mail address."))
    return v


def clean_tg(v: str) -> str | None:
    v = (v or "").strip()
    if not v:
        return None
    if not TG_USER_RE.match(v):
        raise Invalid(N_("Invalid Telegram user name."))
    return "@" + v.lstrip("@")


def sip_digest(ext: str, password: str) -> str:
    return hashlib.md5(f"{ext}:{REALM}:{password}".encode()).hexdigest()


def new_sip_password() -> str:
    # letters and digits only: easy to type into a phone
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


# ---- subscribers ----

def subscribers(db) -> list[dict]:
    return db.all("""SELECT s.*, coalesce(array_agg(m.group_id ORDER BY m.group_id) FILTER (WHERE m.group_id IS NOT NULL), '{}') groups
                     FROM subscribers s LEFT JOIN group_members m ON m.ext = s.ext GROUP BY s.ext ORDER BY s.ext""")


def subscriber(db, ext: str) -> dict | None:
    if not EXT_RE.match(ext or ""):
        return None
    return db.one("""SELECT s.*, coalesce(array_agg(m.group_id ORDER BY m.group_id) FILTER (WHERE m.group_id IS NOT NULL), '{}') groups
                     FROM subscribers s LEFT JOIN group_members m ON m.ext = s.ext WHERE s.ext = %s GROUP BY s.ext""", (ext,))


def number_taken(db, number: str, imap=None, except_group: int | None = None, except_ext: str | None = None) -> bool:
    if db.val("SELECT 1 FROM subscribers WHERE ext = %s AND ext IS DISTINCT FROM %s", (number, except_ext)):
        return True
    if db.val("SELECT 1 FROM groups WHERE number = %s AND id IS DISTINCT FROM %s", (number, except_group)):
        return True
    return bool(imap and number == imap.data().get("test_exten"))


def create_subscriber(db, ext: str, name: str, email: str = "", imap=None) -> str:
    """New subscriber; returns the SIP password (shown once, only its digest is stored)."""
    ext = (ext or "").strip()
    if not EXT_RE.match(ext):
        raise Invalid(N_("The extension must be 2–8 digits."))
    if number_taken(db, ext, imap):
        raise Invalid(N_("This number is already used."))
    pw = new_sip_password()
    db.run("INSERT INTO subscribers (ext, name, email, sip_digest) VALUES (%s, %s, %s, %s)",
           (ext, clean_name(name) or ext, clean_email(email), sip_digest(ext, pw)))
    return pw


def update_subscriber(db, ext: str, *, name=None, email=None, telegram=None, lang=None, max_contacts=None,
                      sip_enabled=None, notify_email=None, notify_telegram=None, notify_direct=None,
                      notify_group=None) -> None:
    sets, args = [], []
    if name is not None:
        sets.append("name = %s")
        args.append(clean_name(name) or ext)
    if email is not None:
        e = clean_email(email)
        if e and db.val("SELECT 1 FROM subscribers WHERE lower(email) = lower(%s) AND ext <> %s", (e, ext)):
            raise Invalid(N_("This e-mail is used by another subscriber."))
        sets.append("email = %s")
        args.append(e)
    if telegram is not None:
        sets.append("telegram = %s")
        args.append(clean_tg(telegram))
    if lang is not None:
        sets.append("lang = %s")
        args.append(lang if lang in ("ru", "en") else None)
    if max_contacts is not None:
        sets.append("max_contacts = %s")
        args.append(min(max(int(max_contacts), 1), 10))
    for col, v in (("sip_enabled", sip_enabled), ("notify_email", notify_email), ("notify_telegram", notify_telegram),
                   ("notify_direct", notify_direct), ("notify_group", notify_group)):
        if v is not None:
            sets.append(f"{col} = %s")
            args.append(bool(v))
    if sets:
        db.run(f"UPDATE subscribers SET {', '.join(sets)}, updated_at = now() WHERE ext = %s", (*args, ext))


def reset_sip_password(db, ext: str) -> str:
    pw = new_sip_password()
    db.run("UPDATE subscribers SET sip_digest = %s, updated_at = now() WHERE ext = %s", (sip_digest(ext, pw), ext))
    return pw


def set_subscriber_groups(db, ext: str, group_ids: list[int]) -> None:
    known = {r["id"] for r in db.all("SELECT id FROM groups")}
    want = [g for g in group_ids if g in known]
    db.run("DELETE FROM group_members WHERE ext = %s AND NOT (group_id = ANY(%s))", (ext, want))
    for g in want:
        db.run("INSERT INTO group_members (group_id, ext, pos) VALUES (%s, %s, "
               "(SELECT coalesce(max(pos), 0) + 1 FROM group_members WHERE group_id = %s)) ON CONFLICT DO NOTHING", (g, ext, g))


def delete_subscriber(db, ext: str) -> None:
    db.run("DELETE FROM subscribers WHERE ext = %s", (ext,))


# ---- groups ----

def groups(db) -> list[dict]:
    return db.all("""SELECT g.*, coalesce(array_agg(m.ext ORDER BY m.pos, m.ext) FILTER (WHERE m.ext IS NOT NULL), '{}') members
                     FROM groups g LEFT JOIN group_members m ON m.group_id = g.id GROUP BY g.id ORDER BY g.id""")


def group(db, gid: int) -> dict | None:
    return db.one("""SELECT g.*, coalesce(array_agg(m.ext ORDER BY m.pos, m.ext) FILTER (WHERE m.ext IS NOT NULL), '{}') members
                     FROM groups g LEFT JOIN group_members m ON m.group_id = g.id WHERE g.id = %s GROUP BY g.id""", (gid,))


def _group_fields(db, gid, name, number, strategy, ring_sec, next_id, notify_email, imap):
    name = clean_name(name, 80)
    if not name:
        raise Invalid(N_("Enter the group name."))
    number = (number or "").strip() or None
    if number and not EXT_RE.match(number):
        raise Invalid(N_("The group number must be 2–8 digits."))
    if number and number_taken(db, number, imap, except_group=gid):
        raise Invalid(N_("This number is already used."))
    if strategy not in STRATEGIES:
        raise Invalid(N_("Unknown ring strategy."))
    try:
        ring_sec = min(max(int(ring_sec), 5), 600)
    except (TypeError, ValueError):
        raise Invalid(N_("Ring time must be a number of seconds.")) from None
    nxt = int(next_id) if str(next_id or "").lstrip("-").isdigit() else None
    if nxt is not None and (nxt == gid or not db.val("SELECT 1 FROM groups WHERE id = %s", (nxt,))):
        nxt = None
    return name, number, strategy, ring_sec, nxt, clean_email(notify_email or "")


def create_group(db, name, number, strategy, ring_sec, next_id=None, notify_email="", imap=None) -> int:
    fields = _group_fields(db, None, name, number, strategy, ring_sec, next_id, notify_email, imap)
    # ids below 100 belong to groups imported from the old system (the IVR refers to them)
    gid = db.val("SELECT greatest(coalesce(max(id), 0) + 1, 100) FROM groups")
    db.run("INSERT INTO groups (id, name, number, strategy, ring_sec, next_id, notify_email) VALUES (%s,%s,%s,%s,%s,%s,%s)",
           (gid, *fields))
    return gid


def update_group(db, gid: int, name, number, strategy, ring_sec, next_id=None, notify_email="", imap=None) -> None:
    fields = _group_fields(db, gid, name, number, strategy, ring_sec, next_id, notify_email, imap)
    db.run("UPDATE groups SET name = %s, number = %s, strategy = %s, ring_sec = %s, next_id = %s, notify_email = %s, "
           "updated_at = now() WHERE id = %s", (*fields, gid))


def set_members(db, gid: int, exts: list[str]) -> None:
    known = {r["ext"] for r in db.all("SELECT ext FROM subscribers")}
    exts = [e for i, e in enumerate(exts) if e in known and e not in exts[:i]]
    db.run("DELETE FROM group_members WHERE group_id = %s AND NOT (ext = ANY(%s))", (gid, exts))
    for pos, e in enumerate(exts, 1):
        db.run("INSERT INTO group_members (group_id, ext, pos) VALUES (%s, %s, %s) "
               "ON CONFLICT (group_id, ext) DO UPDATE SET pos = EXCLUDED.pos", (gid, e, pos))


def ivr_refs(imap, gid: int) -> list[str]:
    """IVR menus (and the IVR exit) that send calls to the group: such a group cannot be deleted."""
    q = f"q{gid}"
    out = [p for p, n in imap.nodes.items() for a in n.get("keys", {}).values() if a.get("target") == q]
    if imap.data().get("exit_queue") == q:
        out.append("exit")
    return sorted(set(out))


def delete_group(db, gid: int, imap) -> None:
    if ivr_refs(imap, gid):
        raise Invalid(N_("The IVR sends calls to this group: change the IVR first."))
    db.run("UPDATE groups SET next_id = NULL WHERE next_id = %s", (gid,))
    db.run("DELETE FROM groups WHERE id = %s", (gid,))


# ---- names for reports ----

def refresh(db, imap) -> None:
    """Pass subscriber names and groups to the IVR map, so reports and the call card show current names."""
    users = {r["ext"]: r["name"] for r in db.all("SELECT ext, name FROM subscribers")}
    queues = {}
    for g in groups(db):
        queues[f"q{g['id']}"] = {"name": g["name"], "number": g["number"], "strategy": g["strategy"], "wait": g["ring_sec"],
                                 "next": f"q{g['next_id']}" if g["next_id"] else f"q{g['id']}", "members": list(g["members"])}
    imap.set_directory(users, queues)
