"""Caller number → country, region and carrier (Google libphonenumber data via the phonenumbers package)."""
from __future__ import annotations

from functools import lru_cache

import phonenumbers
from phonenumbers import carrier, geocoder

INTERNAL = "--"   # pseudo country code for internal extensions


def normalize(num: str, default_region: str = "RU") -> str | None:
    """Digits as the trunk sends them → E.164 (+79161234567) or None if it does not look like a phone number."""
    raw = (num or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 7:
        return None
    if raw.startswith("+"):
        cand = "+" + digits
    elif raw.startswith("00"):
        cand = "+" + digits[2:]
    elif default_region == "RU" and len(digits) == 11 and digits[0] in "78":
        cand = "+7" + digits[1:]
    elif default_region == "RU" and len(digits) == 10:
        cand = "+7" + digits
    else:
        cand = "+" + digits
    try:
        p = phonenumbers.parse(cand, None)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(p):
        return None
    return phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164)


@lru_cache(maxsize=50000)
def lookup(num: str, default_region: str = "RU", internal: frozenset = frozenset()) -> dict:
    """{'e164', 'country', 'region', 'carrier'}; internal extensions → country '--'."""
    out = {"e164": None, "country": None, "region": None, "carrier": None}
    n = (num or "").strip()
    if n in internal or (n.isdigit() and len(n) <= 5):
        out["country"] = INTERNAL
        return out
    e164 = normalize(n, default_region)
    if not e164:
        return out
    p = phonenumbers.parse(e164, None)
    out["e164"] = e164
    out["country"] = phonenumbers.region_code_for_number(p) or None
    out["region"] = geocoder.description_for_number(p, "ru") or None
    out["carrier"] = carrier.name_for_number(p, "ru") or carrier.name_for_number(p, "en") or None
    return out


def pretty(e164: str | None) -> str:
    if not e164:
        return ""
    try:
        return phonenumbers.format_number(phonenumbers.parse(e164, None), phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        return e164
