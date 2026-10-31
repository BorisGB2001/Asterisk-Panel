"""Notifications: missed calls by e-mail and Telegram; the Telegram bot that links chats to subscribers and groups.

Who is notified about a call that nobody answered:
  - the subscriber whose extension was dialled directly (notify_direct);
  - subscribers of the groups the call went through who were rung (notify_group);
  - the group itself: its e-mail and its Telegram chat.
Each subscriber chooses the channels (e-mail, Telegram) in the cabinet. The notifications table keeps
what was sent, so a call is never sent twice to the same target and channel.
"""
from __future__ import annotations

import json
import logging
import secrets
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

from . import fmt, i18n, phone
from .fmt import N_
from .i18n import t

log = logging.getLogger("notify")
TOKEN_TTL = 3600


# ---- transports ----

class Telegram:
    def __init__(self, token: str):
        self.token = token

    def api(self, method: str, wait: float = 15, **params) -> dict:
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                                     data=json.dumps(params).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=wait) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read())
            except ValueError:
                data = {"ok": False, "description": f"HTTP {e.code}"}
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram error"))
        return data["result"]

    def send(self, chat_id: int, text: str) -> None:
        self.api("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)


def send_email(opts, to: str, subject: str, body: str) -> None:
    o = opts.all()
    if not o["smtp_host"] or not o["smtp_from"]:
        raise RuntimeError(N_("SMTP is not configured: fill in the server and the sender address in Settings."))
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = o["smtp_from"], to, subject
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if o["smtp_security"] == "ssl":
        s = smtplib.SMTP_SSL(o["smtp_host"], o["smtp_port"], timeout=20, context=ctx)
    else:
        s = smtplib.SMTP(o["smtp_host"], o["smtp_port"], timeout=20)
    try:
        if o["smtp_security"] == "starttls":
            s.starttls(context=ctx)
        if o["smtp_user"]:
            s.login(o["smtp_user"], o["smtp_password"])
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except smtplib.SMTPException:
            pass


def bot(opts) -> Telegram | None:
    tok = opts.get("tg_token")
    return Telegram(tok) if tok else None


# ---- links: t.me/<bot>?start=<token> ----

def link_token(db, kind: str, ref: str) -> str:
    db.run("DELETE FROM tg_tokens WHERE expires_at < now() OR (kind = %s AND ref = %s)", (kind, ref))
    tok = secrets.token_urlsafe(18)
    db.run("INSERT INTO tg_tokens (token, kind, ref, expires_at) VALUES (%s, %s, %s, now() + %s * interval '1 second')",
           (tok, kind, ref, TOKEN_TTL))
    return tok


def link_url(opts, db, kind: str, ref: str) -> str | None:
    name = opts.stored().get("tg_bot") or ""
    if not name or not opts.get("tg_token"):
        return None
    tok = link_token(db, kind, ref)
    return f"https://t.me/{name}?{'startgroup' if kind == 'group' else 'start'}={tok}"


# ---- messages ----

def panel_url(opts, settings) -> str:
    url = opts.get("panel_url")
    if url:
        return url.rstrip("/")
    return f"https://{settings.panel_host}:{settings.panel_port}" if settings.panel_host else ""


def who_called(c: dict, users: dict) -> str:
    num = c.get("caller_num") or t("unknown number")
    if c.get("caller_country") == phone.INTERNAL or num in users:
        return f"{num} · {users.get(num, c.get('caller_name') or '')}".rstrip(" ·")
    text = phone.pretty(c.get("caller_e164")) or num
    if c.get("caller_name") and c["caller_name"] != num:
        text += f" ({c['caller_name']})"
    place = ", ".join(x for x in (fmt.country(c["caller_country"]) if c.get("caller_country") else "",
                                  c.get("caller_region") or "") if x)
    return text + (f" — {place}" if place else "")


def missed_text(c: dict, imap, base: str, path: str) -> tuple[str, str]:
    users = imap.users
    lines = [t("From: {who}", who=who_called(c, users)),
             t("Time: {time}", time=fmt.dt(c["start_at"], "%d.%m.%Y %H:%M"))]
    if c.get("queues"):
        lines.append(t("Groups: {names}", names=", ".join(imap.queue_title(q) for q in c["queues"])))
    elif c.get("dst"):
        lines.append(t("To: {who}", who=imap.user_title(c["dst"])))
    if c.get("wait_sec"):
        lines.append(t("Waited: {n} s", n=c["wait_sec"]))
    if base:
        lines.append(base + path + c["linkedid"])
    return t("Missed call"), "\n".join(lines)


class Notifier:
    def __init__(self, db, opts, imap, settings):
        self.db, self.opts, self.imap, self.settings = db, opts, imap, settings
        self.tg_ok = False
        self.tg_error = ""

    # which subscribers and groups to notify about a call
    def targets(self, c: dict) -> list[dict]:
        if c["status"] not in ("missed", "unanswered"):
            return []
        out: dict[str, dict] = {}
        subs = {r["ext"]: r for r in self.db.all("SELECT * FROM subscribers")}
        if c["direction"] == "internal" and c.get("dst") in subs and c["dst"] != c.get("source"):
            s = subs[c["dst"]]
            if s["notify_direct"]:
                out[f"sub:{s['ext']}"] = self._sub(s)
        for q in c.get("queues") or []:
            if not q.startswith("q") or not q[1:].isdigit():
                continue
            g = self.db.one("SELECT * FROM groups WHERE id = %s", (int(q[1:]),))
            if not g:
                continue
            if g["notify_email"] or g["tg_chat_id"]:
                out.setdefault(f"group:{g['id']}", {"email": g["notify_email"], "chat": g["tg_chat_id"], "lang": None,
                                                    "path": "/call/"})
            members = {r["ext"] for r in self.db.all("SELECT ext FROM group_members WHERE group_id = %s", (g["id"],))}
            for ext in c.get("rung") or []:
                s = subs.get(ext)
                if s and ext in members and s["notify_group"]:
                    out.setdefault(f"sub:{ext}", self._sub(s))
        return [{"target": k, **v} for k, v in out.items()]

    @staticmethod
    def _sub(s: dict) -> dict:
        return {"email": s["email"] if s["notify_email"] else None, "chat": s["tg_chat_id"] if s["notify_telegram"] else None,
                "lang": s["lang"], "path": "/me/call/"}

    def send(self, target: dict, subject: str, body: str, linkedid: str | None) -> None:
        tg = bot(self.opts)
        for channel, addr in (("email", target.get("email")), ("telegram", target.get("chat"))):
            if not addr or (channel == "telegram" and not tg):
                continue
            if linkedid and self.db.val("SELECT 1 FROM notifications WHERE linkedid = %s AND target = %s AND channel = %s",
                                        (linkedid, target["target"], channel)):
                continue
            err = None
            try:
                if channel == "email":
                    send_email(self.opts, addr, subject, body)
                else:
                    tg.send(int(addr), f"📞 {subject}\n{body}")
            except Exception as e:  # noqa: BLE001 — SMTP/Telegram errors are logged per notification
                err = str(e)[:300]
                log.warning("%s %s: %s", channel, target["target"], err)
            self.db.run("INSERT INTO notifications (linkedid, target, channel, status, error) VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING", (linkedid, target["target"], channel, "failed" if err else "sent", err))

    def step(self) -> int:
        rows = self.db.all("SELECT * FROM calls WHERE NOT notify_done ORDER BY start_at LIMIT 50")
        enabled = self.opts.get("notify_enabled")
        base = panel_url(self.opts, self.settings)
        for c in rows:
            try:
                for tg in self.targets(c) if enabled else []:
                    with i18n.use(tg["lang"] or i18n.DEFAULT):
                        subject, body = missed_text(c, self.imap, base, tg["path"])
                    self.send(tg, subject, body, c["linkedid"])
            except Exception:  # noqa: BLE001 — one call must not block the queue
                log.exception("notify %s", c["linkedid"])
            self.db.run("UPDATE calls SET notify_done = true WHERE linkedid = %s", (c["linkedid"],))
        return len(rows)

    def test(self, target: dict) -> list[str]:
        """Test message to a subscriber or a group; returns errors."""
        with i18n.use(target.get("lang") or i18n.DEFAULT):
            subject, body = t("Test notification"), t("Notifications from the Asterisk panel work.")
        before = self.db.val("SELECT coalesce(max(id), 0) FROM notifications")
        self.send(target, subject, body, None)
        return [f"{r['channel']}: {r['error']}" for r in
                self.db.all("SELECT channel, error FROM notifications WHERE id > %s AND status = 'failed'", (before,))]

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.step()
            except Exception:  # noqa: BLE001
                log.exception("notifier")
            stop.wait(10)

    # ---- Telegram bot: /start <token> links the chat ----

    def handle(self, tg: Telegram, upd: dict) -> None:
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        if not text.startswith("/") or not chat.get("id"):
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0]
        lang = (msg.get("from") or {}).get("language_code")
        with i18n.use("ru" if (lang or "").startswith("ru") else "en"):
            if cmd == "/start" and arg:
                row = self.db.one("DELETE FROM tg_tokens WHERE token = %s AND expires_at > now() RETURNING kind, ref",
                                  (arg.strip()[:64],))
                if not row:
                    tg.send(chat["id"], t("The link is out of date. Open a new one in the panel."))
                    return
                if row["kind"] == "sub":
                    self.db.run("UPDATE subscribers SET tg_chat_id = NULL WHERE tg_chat_id = %s", (chat["id"],))
                    self.db.run("UPDATE subscribers SET tg_chat_id = %s, tg_linked_at = now(), "
                                "telegram = coalesce(%s, telegram) WHERE ext = %s",
                                (chat["id"], "@" + msg["from"]["username"] if (msg.get("from") or {}).get("username") else None,
                                 row["ref"]))
                    name = self.db.val("SELECT name FROM subscribers WHERE ext = %s", (row["ref"],)) or row["ref"]
                    tg.send(chat["id"], t("Done: missed calls for {who} will come here.", who=f"{row['ref']} · {name}"))
                else:
                    self.db.run("UPDATE groups SET tg_chat_id = %s, tg_title = %s WHERE id = %s",
                                (chat["id"], (chat.get("title") or chat.get("username") or "")[:100], int(row["ref"])))
                    name = self.db.val("SELECT name FROM groups WHERE id = %s", (int(row["ref"]),))
                    tg.send(chat["id"], t("Done: missed calls of the group {name} will come to this chat.", name=name))
                self.db.event("telegram_linked", {"kind": row["kind"], "ref": row["ref"]}, actor="telegram")
            elif cmd == "/start":
                tg.send(chat["id"], t("To get notifications, open the Telegram link from your personal cabinet."))
            elif cmd == "/stop":
                n = self.db.run("UPDATE subscribers SET tg_chat_id = NULL WHERE tg_chat_id = %s", (chat["id"],))
                n += self.db.run("UPDATE groups SET tg_chat_id = NULL WHERE tg_chat_id = %s", (chat["id"],))
                tg.send(chat["id"], t("Notifications to this chat are off.") if n else t("This chat was not linked."))

    def poll(self, stop: threading.Event) -> None:
        offset, token = 0, None
        while not stop.is_set():
            tg = bot(self.opts)
            if not tg:
                self.tg_ok, self.tg_error = False, ""
                stop.wait(15)
                continue
            try:
                if tg.token != token:
                    me = tg.api("getMe")
                    if self.opts.stored().get("tg_bot") != me.get("username"):
                        self.opts.put("tg_bot", me.get("username"), "telegram")
                    token, offset = tg.token, 0
                for upd in tg.api("getUpdates", 40, offset=offset, timeout=25, allowed_updates=["message"]):
                    offset = upd["update_id"] + 1
                    try:
                        self.handle(tg, upd)
                    except Exception:  # noqa: BLE001
                        log.exception("telegram update")
                self.tg_ok, self.tg_error = True, ""
            except Exception as e:  # noqa: BLE001 — no network, bad token: show on the Settings page, retry
                self.tg_ok, self.tg_error, token = False, str(e)[:200], None
                stop.wait(30)
            time.sleep(0.2)
