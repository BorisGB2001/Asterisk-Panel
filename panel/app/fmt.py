"""Formatting for templates: dates in the panel time zone, durations, country flags and names, statuses."""
from __future__ import annotations

import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import phonenumbers
from phonenumbers import geocoder

from . import __version__, i18n
from .config import settings
from .i18n import t

TZ = ZoneInfo(settings.tz)


def N_(s: str) -> str:
    """Mark a phrase for translation without translating it here (translated when shown, via t)."""
    return s


STATUS_NAMES = {"answered": N_("answered"), "missed": N_("missed"), "ivr": N_("left in IVR"),
                "unanswered": N_("not answered")}
DIRECTION_NAMES = {"inbound": N_("inbound"), "internal": N_("internal")}
DEVICE_LOG_NAMES = {"register": N_("registered"), "ip_change": N_("IP changed"), "unregister": N_("unregistered"),
                    "reachable": N_("reachable"), "unreachable": N_("unreachable"), "auth_failed": N_("failed sign-in")}
EVENT_NAMES = {"admin_login": N_("panel sign-in"), "admin_login_failed": N_("failed panel sign-in"),
               "acct_create": N_("administrator added"), "acct_reset": N_("administrator password reset"),
               "acct_disable": N_("administrator disabled"), "acct_enable": N_("administrator enabled"),
               "acct_password": N_("password changed"), "acct_totp_bound": N_("authenticator set up"),
               "recording_played": N_("recording played"), "calls_exported": N_("calls exported")}
ROLE_NAMES = {"super": N_("super administrator"), "viewer": N_("viewer")}
WEEKDAYS = (N_("Mon"), N_("Tue"), N_("Wed"), N_("Thu"), N_("Fri"), N_("Sat"), N_("Sun"))


def local(ts) -> datetime.datetime | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.datetime.fromtimestamp(ts, TZ)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=datetime.timezone.utc)
    return ts.astimezone(TZ)


def dt(ts, fmt: str = "%d.%m.%Y %H:%M:%S") -> str:
    v = local(ts)
    return v.strftime(fmt) if v else "—"


def hms(sec) -> str:
    """Duration 0:42, 12:05, 1:02:03."""
    if sec is None:
        return "—"
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def hours(sec) -> str:
    sec = int(sec or 0)
    return t("{h} h {m} min", h=sec // 3600, m=sec % 3600 // 60) if sec >= 3600 else t("{m} min", m=sec // 60)


def pct(a, b) -> str:
    return f"{100 * (a or 0) / b:.0f} %" if b else "—"


def flag(cc: str | None) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc.upper()) if cc and len(cc) == 2 and cc.isalpha() else ""


@lru_cache(maxsize=1000)
def _country_name(cc: str, lang: str) -> str:
    ex = phonenumbers.example_number(cc)
    name = geocoder.country_name_for_number(ex, lang) if ex else ""
    return name or cc


def country(cc: str | None) -> str:
    if not cc:
        return t("unknown")
    if cc == "--":
        return t("internal")
    return _country_name(cc.upper(), i18n.get())


def status_name(key: str | None) -> str:
    return t(STATUS_NAMES.get(key or "", key or "—"))


def event_name(kind: str) -> str:
    return t(EVENT_NAMES[kind]) if kind in EVENT_NAMES else kind


def register(env) -> None:
    env.filters.update(dt=dt, hms=hms, hours=hours, flag=flag, country=country)
    env.globals.update(_=t, N_=N_, VERSION=__version__, status_name=status_name, pct=pct, event_name=event_name,
                       ROLE_NAMES=ROLE_NAMES, DIRECTION_NAMES=DIRECTION_NAMES, DEVICE_LOG_NAMES=DEVICE_LOG_NAMES,
                       STATUS_NAMES=STATUS_NAMES, languages=i18n.languages)
