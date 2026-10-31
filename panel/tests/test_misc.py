import datetime

from app import fmt, phone, reports
from app.ami import ADDR_RE, parse_uri


def test_phone_ru_formats():
    for raw in ("79161234567", "89161234567", "+7 916 123-45-67", "9161234567"):
        info = phone.lookup(raw)
        assert info["e164"] == "+79161234567" and info["country"] == "RU", raw
    assert phone.lookup("74951234567")["region"]   # Moscow landline → region known


def test_phone_foreign_and_internal():
    assert phone.lookup("+375291234567")["country"] == "BY"
    assert phone.lookup("00491701234567")["country"] == "DE"
    assert phone.lookup("2204", internal=frozenset({"2204"}))["country"] == phone.INTERNAL
    assert phone.lookup("anonymous")["country"] is None


def test_country_names():
    from app import i18n
    with i18n.use("ru"):
        assert fmt.country("RU") == "Россия"
        assert fmt.country("--") == "внутренний"
    assert fmt.flag("RU") == "🇷🇺"


def test_periods():
    now = datetime.datetime(2026, 10, 31, 12, 0, tzinfo=fmt.TZ)
    p = reports.period("today", now=now)
    assert p.since.hour == 0 and (p.until - p.since).days == 1 and p.bucket == "hour"
    p = reports.period("7d", now=now)
    assert (p.until - p.since).days == 7 and p.bucket == "day"
    p = reports.period("prev", now=now)
    assert p.since.month == 9 and p.until.month == 10 and p.until.day == 1
    p = reports.period("custom", "2026-10-01", "2026-10-03", now=now)
    assert (p.until - p.since).days == 3 and "custom" in p.query()


def test_ami_parsers():
    assert parse_uri("sip:2204@192.0.2.10:59920;ob") == ("192.0.2.10", 59920)
    assert parse_uri("sip:2204@[2001:db8::1]:5060") == ("2001:db8::1", 5060)
    m = ADDR_RE.match("IPV4/UDP/203.0.113.5/5060")
    assert m.group("ip") == "203.0.113.5"


def test_templates_compile():
    from app.main import templates
    env = templates.env
    for name in env.list_templates():
        env.get_template(name)
