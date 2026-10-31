"""Settings edited on the Settings page (table panel.settings): Asterisk SIP/RTP/groups, reports, notifications.

Each option has a type, a default and a validator. Values are checked strictly because many of them end up in
Asterisk config files (see provision.py): a stray newline or ";" must never get there.
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from .fmt import N_

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
CODECS_RE = re.compile(r"^[a-z0-9_]{2,16}(,[a-z0-9_]{2,16}){0,9}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$")
EMAIL_RE = re.compile(r"^[^@\s;,<>\"']{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
URL_RE = re.compile(r"^https?://[A-Za-z0-9.:\[\]-]{1,260}(/[\w./-]*)?$")
TG_TOKEN_RE = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{30,64}$")
REGION_RE = re.compile(r"^[A-Z]{2}$")


@dataclass(frozen=True)
class Opt:
    key: str
    section: str
    label: str
    kind: str                 # int | str | bool | choice | secret | list
    default: object
    help: str = ""
    lo: int = 0
    hi: int = 0
    choices: tuple = ()
    check: object = None      # callable(str) -> bool for str/list items
    apply: str = ""           # reload | restart | "" (panel only)


def _ip_or_host(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return bool(HOST_RE.match(v))


def _ip(v: str) -> bool:
    try:
        ipaddress.ip_address(v)
        return True
    except ValueError:
        return False


def _cidr(v: str) -> bool:
    try:
        ipaddress.ip_network(v, strict=False)
        return True
    except ValueError:
        return False


SECTIONS = {"sip": N_("SIP and phones"), "rtp": N_("Media (RTP)"), "groups": N_("Groups and calls"),
            "reports": N_("Reports"), "smtp": N_("E-mail (SMTP)"), "telegram": N_("Telegram"),
            "notify": N_("Notifications")}

OPTIONS = [
    Opt("sip_bind", "sip", N_("Listen address"), "str", "0.0.0.0", N_("0.0.0.0 — all interfaces"), check=_ip, apply="restart"),
    Opt("sip_port", "sip", N_("SIP port (UDP)"), "int", 5060, lo=1, hi=65535, apply="restart"),
    Opt("external_address", "sip", N_("External address"), "str", "",
        N_("Public IP or name, if the server is behind NAT and phones connect from the internet. Empty — not behind NAT."),
        check=_ip_or_host, apply="restart"),
    Opt("local_net", "sip", N_("Local networks"), "list", ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
        N_("Networks that are not behind NAT, one per line (used together with the external address)."), check=_cidr,
        apply="restart"),
    Opt("codecs", "sip", N_("Codecs"), "str", "alaw,ulaw", N_("In order of preference, comma separated."),
        check=lambda v: bool(CODECS_RE.match(v)), apply="reload"),
    Opt("language", "sip", N_("Phone language"), "choice", "ru", N_("Language of Asterisk system prompts."),
        choices=("ru", "en"), apply="reload"),
    Opt("max_contacts", "sip", N_("Devices per account"), "int", 3, N_("How many phones may register with one account."),
        lo=1, hi=10, apply="reload"),
    Opt("qualify_frequency", "sip", N_("Phone check interval, s"), "int", 30, N_("0 — do not check whether phones are reachable."),
        lo=0, hi=3600, apply="reload"),
    Opt("moh_hold", "sip", N_("Hold music class"), "str", "bx-hold", check=lambda v: bool(NAME_RE.match(v)), apply="reload"),
    Opt("rtp_start", "rtp", N_("First RTP port"), "int", 10000, lo=1024, hi=65000, apply="reload"),
    Opt("rtp_end", "rtp", N_("Last RTP port"), "int", 20000, lo=1024, hi=65535, apply="reload"),
    Opt("ring_ext_sec", "groups", N_("Ring an extension, s"), "int", 30, N_("Direct call to an extension before giving up."),
        lo=5, hi=300, apply="reload"),
    Opt("group_ring_sec", "groups", N_("Ring a new group, s"), "int", 20, N_("Default for new groups."), lo=5, hi=600),
    Opt("max_hops", "groups", N_("Max passes between groups"), "int", 30,
        N_("Protection against an endless ring of groups (A → B → A …): after that many passes the call ends."),
        lo=1, hi=200, apply="reload"),
    Opt("moh_wait", "groups", N_("Music while operators are rung"), "str", "bx-wait", check=lambda v: bool(NAME_RE.match(v)),
        apply="reload"),
    Opt("moh_busy", "groups", N_("Music when all operators are busy"), "str", "bx-enqueue",
        check=lambda v: bool(NAME_RE.match(v)), apply="reload"),
    Opt("pickup", "groups", N_("Call pickup code"), "str", "*8", check=lambda v: bool(re.match(r"^\*?[0-9]{1,4}$", v)),
        apply="reload"),
    Opt("sla_seconds", "reports", N_("SLA, s"), "int", None, N_("Answered within this time counts as within SLA."), lo=1, hi=600),
    Opt("default_region", "reports", N_("Country of national numbers"), "str", None,
        N_("Two-letter code (RU): numbers without a country code are treated as numbers of this country."),
        check=lambda v: bool(REGION_RE.match(v))),
    Opt("smtp_host", "smtp", N_("SMTP server"), "str", "", check=lambda v: bool(HOST_RE.match(v))),
    Opt("smtp_port", "smtp", N_("Port"), "int", 587, lo=1, hi=65535),
    Opt("smtp_security", "smtp", N_("Encryption"), "choice", "starttls", choices=("starttls", "ssl", "none")),
    Opt("smtp_user", "smtp", N_("User"), "str", "", check=lambda v: len(v) <= 200 and "\n" not in v),
    Opt("smtp_password", "smtp", N_("Password"), "secret", ""),
    Opt("smtp_from", "smtp", N_("Sender address"), "str", "", check=lambda v: bool(EMAIL_RE.match(v))),
    Opt("tg_token", "telegram", N_("Bot token"), "secret", "",
        N_("From @BotFather. The server needs access to api.telegram.org."), check=lambda v: bool(TG_TOKEN_RE.match(v))),
    Opt("notify_enabled", "notify", N_("Send notifications"), "bool", True),
    Opt("panel_url", "notify", N_("Panel address in messages"), "str", "",
        N_("Links in notifications. Empty — https://PANEL_HOST:PANEL_PORT."), check=lambda v: bool(URL_RE.match(v))),
]
BY_KEY = {o.key: o for o in OPTIONS}
STRATEGIES = ("ringall", "rrmemory", "leastrecent", "fewestcalls", "random", "linear", "wrandom")
STRATEGY_NAMES = {"ringall": N_("all at once"), "rrmemory": N_("in turn (round robin)"),
                  "leastrecent": N_("who answered longest ago"), "fewestcalls": N_("who answered fewest calls"),
                  "random": N_("random"), "linear": N_("in list order"), "wrandom": N_("random by weight")}


class Options:
    """Values from panel.settings over the defaults; cached for a few seconds (pages read them on every request)."""

    def __init__(self, db, settings):
        self.db, self.env = db, settings
        self._cache: dict = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def default(self, key: str):
        o = BY_KEY[key]
        if o.default is None:     # comes from .env
            return {"sla_seconds": self.env.sla_seconds, "default_region": self.env.default_region}[key]
        return list(o.default) if isinstance(o.default, list) else o.default

    def stored(self) -> dict:
        with self._lock:
            if time.time() - self._at > 5:
                self._cache = {r["key"]: r["value"] for r in self.db.all("SELECT key, value FROM settings")}
                self._at = time.time()
            return self._cache

    def __getattr__(self, key: str):
        # opts.default_region, opts.sla_seconds — the same interface as config.settings for calls.build_row
        if key in BY_KEY:
            return self.get(key)
        raise AttributeError(key)

    def get(self, key: str):
        v = self.stored().get(key)
        return self.default(key) if v is None else v

    def all(self) -> dict:
        return {o.key: self.get(o.key) for o in OPTIONS}

    def is_set(self, key: str) -> bool:
        return bool(self.stored().get(key))

    def set_many(self, values: dict, actor: str) -> None:
        for k, v in values.items():
            self.db.run("INSERT INTO settings (key, value, updated_by) VALUES (%s, %s, %s) ON CONFLICT (key) DO UPDATE "
                        "SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
                        (k, Jsonb(v), actor))
        self.invalidate()

    def put(self, key: str, value, actor: str = "panel") -> None:
        self.set_many({key: value}, actor)

    def invalidate(self) -> None:
        with self._lock:
            self._at = 0.0

    # ---- form handling ----

    def parse(self, form: dict) -> tuple[dict, list[str]]:
        """Form fields → changed values and the list of option labels with errors. Empty secret = keep the old one."""
        out, bad = {}, []
        for o in OPTIONS:
            if o.kind == "bool":
                v = form.get(o.key) == "on"
            else:
                raw = str(form.get(o.key, "")).strip()
                if o.kind == "secret":
                    if form.get(o.key + "__clear") == "on":
                        out[o.key] = ""
                        continue
                    if not raw:
                        continue
                    if o.check and not o.check(raw) or len(raw) > 200 or "\n" in raw:
                        bad.append(o.label)
                        continue
                    v = raw
                elif o.kind == "int":
                    if raw == "" and o.default is None:
                        v = self.default(o.key)
                    else:
                        try:
                            v = int(raw)
                        except ValueError:
                            bad.append(o.label)
                            continue
                        if not o.lo <= v <= o.hi:
                            bad.append(o.label)
                            continue
                elif o.kind == "choice":
                    if raw not in o.choices:
                        bad.append(o.label)
                        continue
                    v = raw
                elif o.kind == "list":
                    items = [x.strip() for x in re.split(r"[\s,]+", raw) if x.strip()]
                    if len(items) > 20 or any(o.check and not o.check(x) for x in items):
                        bad.append(o.label)
                        continue
                    v = items
                else:
                    if raw == "" and o.default is None:
                        v = self.default(o.key)
                    elif raw and o.check and not o.check(raw) or len(raw) > 300:
                        bad.append(o.label)
                        continue
                    else:
                        v = raw.upper() if o.key == "default_region" else raw
            if v != self.get(o.key):
                out[o.key] = v
        if BY_KEY["rtp_start"].label not in bad and BY_KEY["rtp_end"].label not in bad:
            a, b = out.get("rtp_start", self.get("rtp_start")), out.get("rtp_end", self.get("rtp_end"))
            if b - a < 100:
                bad.append(BY_KEY["rtp_end"].label)
        return out, bad
