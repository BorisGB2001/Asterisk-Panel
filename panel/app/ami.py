"""Asterisk Manager Interface (AMI, 127.0.0.1:5038).

AMI.action() — short request/response for pages (live channels, contacts).
Collector — long-lived connection: phone registrations (ContactStatus), failed sign-ins (security events)
→ panel.devices and panel.device_log, with IP geolocation.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from urllib.parse import unquote

from psycopg.types.json import Jsonb

log = logging.getLogger("ami")
URI_RE = re.compile(r"^sips?:(?:[^@]*@)?\[?(?P<host>[0-9a-fA-F.:]+?)\]?(?::(?P<port>\d+))?(?:;.*)?$")
ADDR_RE = re.compile(r"^IPV[46]/(?:UDP|TCP|TLS|WS|WSS)/(?P<ip>[^/]+)/(?P<port>\d+)$")


def parse_uri(uri: str | None) -> tuple[str | None, int | None]:
    m = URI_RE.match(unquote(uri or ""))
    if not m:
        return None, None
    return m.group("host"), int(m.group("port")) if m.group("port") else None


class AMIError(Exception):
    pass


class Conn:
    def __init__(self, host: str, port: int, user: str, secret: str, timeout: float = 5.0, events: bool = False):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self.banner = self._line()
        self.seq = 0
        resp = self.send({"Action": "Login", "Username": user, "Secret": secret, "Events": "on" if events else "off"})
        if resp.get("Response") != "Success":
            raise AMIError(resp.get("Message", "login failed"))

    def _line(self) -> str:
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("AMI closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line.decode("utf-8", "replace")

    def read(self) -> dict:
        msg: dict[str, str] = {}
        while True:
            line = self._line()
            if line == "":
                if msg:
                    return msg
                continue
            k, _, v = line.partition(":")
            k = k.strip()
            # Command answers with one "Output:" header per line of text
            msg[k] = msg[k] + "\n" + v.strip() if k == "Output" and k in msg else v.strip()

    def send(self, action: dict) -> dict:
        self.seq += 1
        aid = f"p{self.seq}"
        payload = "".join(f"{k}: {v}\r\n" for k, v in {**action, "ActionID": aid}.items()) + "\r\n"
        self.sock.sendall(payload.encode())
        while True:
            m = self.read()
            if m.get("ActionID") == aid and "Response" in m:
                return m

    def collect(self, action: dict, item_event: str, done_event: str) -> list[dict]:
        """Actions that answer with a list of events (CoreShowChannels, PJSIPShowContacts…)."""
        resp = self.send(action)
        if resp.get("Response") != "Success":
            raise AMIError(resp.get("Message", "action failed"))
        out = []
        while True:
            m = self.read()
            ev = m.get("Event")
            if ev == item_event:
                out.append(m)
            elif ev == done_event:
                return out

    def close(self) -> None:
        try:
            self.sock.sendall(b"Action: Logoff\r\n\r\n")
        except OSError:
            pass
        self.sock.close()


class AMI:
    def __init__(self, settings):
        self.s = settings

    def _conn(self, events: bool = False, timeout: float = 5.0) -> Conn:
        return Conn(self.s.ami_host, self.s.ami_port, self.s.ami_user, self.s.ami_secret, timeout, events)

    def channels(self) -> list[dict]:
        c = self._conn()
        try:
            return c.collect({"Action": "CoreShowChannels"}, "CoreShowChannel", "CoreShowChannelsComplete")
        finally:
            c.close()

    def contacts(self) -> list[dict]:
        c = self._conn()
        try:
            return c.collect({"Action": "PJSIPShowContacts"}, "ContactList", "ContactListComplete")
        finally:
            c.close()

    def action(self, action: dict) -> dict:
        c = self._conn()
        try:
            resp = c.send(action)
        finally:
            c.close()
        if resp.get("Response") != "Success":
            raise AMIError(resp.get("Message", "action failed"))
        return resp

    def reload(self, module: str) -> None:
        self.action({"Action": "Reload", "Module": module})

    def command(self, cmd: str) -> str:
        """CLI command (needs write=command for the AMI user); returns the output."""
        c = self._conn(timeout=10)
        try:
            resp = c.send({"Action": "Command", "Command": cmd})
        finally:
            c.close()
        if resp.get("Response") not in ("Success", "Follows"):
            raise AMIError(resp.get("Message", "command failed"))
        return resp.get("Output", "")

    def ping(self) -> bool:
        try:
            self._conn(timeout=2).close()
            return True
        except (OSError, AMIError):
            return False


SECURITY_FAIL = {"InvalidAccountID", "InvalidPassword", "ChallengeResponseFailed", "FailedACL", "UnexpectedAddress",
                 "RequestNotAllowed", "AuthMethodNotAllowed"}


class Collector:
    """Keeps panel.devices current and writes panel.device_log; reconnects on its own."""

    def __init__(self, settings, db, geo):
        self.s, self.db, self.geo = settings, db, geo
        self.connected = False

    def _geo(self, ip: str | None) -> dict:
        return self.geo.lookup(ip) if ip and self.geo else {"country": None, "city": None, "org": None}

    def contact(self, endpoint: str, uri: str, status: str, user_agent: str | None, rtt_usec: str | None) -> None:
        if not endpoint:
            return
        ip, port = parse_uri(uri)
        g = self._geo(ip)
        rtt = round(int(rtt_usec) / 1000, 1) if (rtt_usec or "").isdigit() and int(rtt_usec) > 0 else None
        prev = self.db.one("SELECT ip, port, status FROM devices WHERE endpoint = %s", (endpoint,))
        removed = status == "Removed"
        self.db.run("""INSERT INTO devices (endpoint, uri, ip, port, user_agent, status, rtt_ms, country, city, org,
                         registered_at, updated_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN NULL ELSE now() END, now())
                       ON CONFLICT (endpoint) DO UPDATE SET uri = EXCLUDED.uri, ip = COALESCE(EXCLUDED.ip, devices.ip),
                         port = COALESCE(EXCLUDED.port, devices.port), user_agent = COALESCE(EXCLUDED.user_agent, devices.user_agent),
                         status = EXCLUDED.status, rtt_ms = COALESCE(EXCLUDED.rtt_ms, devices.rtt_ms),
                         country = COALESCE(EXCLUDED.country, devices.country), city = COALESCE(EXCLUDED.city, devices.city),
                         org = COALESCE(EXCLUDED.org, devices.org),
                         registered_at = CASE WHEN %s THEN NULL
                                              WHEN devices.registered_at IS NULL OR devices.status = 'Removed' THEN now()
                                              ELSE devices.registered_at END,
                         updated_at = now()""",
                    (endpoint, uri, ip, port, user_agent or None, status, rtt, g["country"], g["city"], g["org"], removed, removed))
        kind = None
        if removed:
            kind = "unregister"
        elif prev is None or prev["status"] == "Removed":
            kind = "register"
        elif ip and (prev["ip"], prev["port"]) != (ip, port):
            kind = "ip_change"
        elif status in ("Reachable", "Unreachable") and prev["status"] != status and prev["status"] in ("Reachable", "Unreachable"):
            kind = status.lower()
        if kind:
            detail = {"from": f"{prev['ip']}:{prev['port']}"} if kind == "ip_change" and prev else None
            self.db.run("INSERT INTO device_log (endpoint, kind, ip, port, user_agent, country, city, org, detail) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (endpoint, kind, ip, port, user_agent or None, g["country"], g["city"], g["org"],
                         None if detail is None else Jsonb(detail)))

    def security(self, m: dict) -> None:
        if m.get("Event") not in SECURITY_FAIL:
            return
        a = ADDR_RE.match(m.get("RemoteAddress", ""))
        ip, port = (a.group("ip"), int(a.group("port"))) if a else (None, None)
        g = self._geo(ip)
        self.db.run("INSERT INTO device_log (endpoint, kind, ip, port, country, city, org, detail) "
                    "VALUES (%s,'auth_failed',%s,%s,%s,%s,%s,%s)",
                    (m.get("AccountID") or None, ip, port, g["country"], g["city"], g["org"],
                     Jsonb({"event": m.get("Event"), "service": m.get("Service")})))

    def sync(self, c: Conn) -> None:
        seen = set()
        for m in c.collect({"Action": "PJSIPShowContacts"}, "ContactList", "ContactListComplete"):
            ep = m.get("Endpoint") or m.get("EndpointName") or (m.get("ObjectName") or "").split(";@")[0]
            seen.add(ep)
            self.contact(ep, m.get("Uri") or m.get("URI", ""), m.get("Status", "Reachable"), m.get("UserAgent"),
                         m.get("RoundtripUsec"))
        for r in self.db.all("SELECT endpoint FROM devices WHERE status <> 'Removed'"):
            if r["endpoint"] not in seen:
                self.contact(r["endpoint"], "", "Removed", None, None)

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            c = None
            try:
                c = Conn(self.s.ami_host, self.s.ami_port, self.s.ami_user, self.s.ami_secret, timeout=10, events=True)
                c.sock.settimeout(None)
                self.sync(c)
                self.connected = True
                log.info("AMI connected")
                while not stop.is_set():
                    m = c.read()
                    ev = m.get("Event")
                    if ev == "ContactStatus":
                        self.contact(m.get("EndpointName", ""), m.get("URI", ""), m.get("ContactStatus", ""),
                                     m.get("UserAgent"), m.get("RoundtripUsec"))
                    elif ev in SECURITY_FAIL:
                        self.security(m)
            except Exception as e:  # noqa: BLE001 — Asterisk restarted, AMI disabled…: retry
                if self.connected:
                    log.warning("AMI: %s", e)
                self.connected = False
            finally:
                if c:
                    try:
                        c.sock.close()
                    except OSError:
                        pass
            stop.wait(10)
