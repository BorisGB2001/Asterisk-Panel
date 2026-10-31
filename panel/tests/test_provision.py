import hashlib

from app import directory, provision
from app.config import settings
from app.options import Options


class FakeDB:
    def __init__(self, stored=None):
        self.stored = stored or {}

    def all(self, sql, args=()):
        if "FROM settings" in sql:
            return [{"key": k, "value": v} for k, v in self.stored.items()]
        return []


SUBS = [
    {"ext": "2204", "name": 'Op "One" <x>;[evil]', "sip_enabled": True, "sip_digest": "ab" * 16, "max_contacts": 2},
    {"ext": "2205", "name": "Off", "sip_enabled": False, "sip_digest": "cd" * 16, "max_contacts": 3},
]
GROUPS = [
    {"id": 3, "name": "Operators", "number": "9992", "strategy": "ringall", "ring_sec": 20, "next_id": 11,
     "members": ["2204", "2205"]},
    {"id": 11, "name": "Seniors", "number": None, "strategy": "rrmemory", "ring_sec": 35, "next_id": None, "members": []},
]


def render(monkeypatch, stored=None):
    monkeypatch.setattr(directory, "subscribers", lambda db: SUBS)
    monkeypatch.setattr(directory, "groups", lambda db: GROUPS)
    db = FakeDB(stored)
    return provision.render(db, Options(db, settings))


def test_render_pjsip(monkeypatch):
    pj = render(monkeypatch)["pjsip.conf"]
    assert "bind=0.0.0.0:5060" in pj and "external_media_address" not in pj
    assert "[2204](endpoint-tpl)" in pj and "password_digest=MD5:" + "ab" * 16 in pj
    assert "max_contacts=2" in pj
    assert "[2205]" not in pj                      # SIP disabled → no endpoint
    cid = next(x for x in pj.splitlines() if x.startswith("callerid="))
    assert cid == 'callerid="Op One xevil" <2204>'  # quotes, brackets and ";" removed


def test_render_nat_and_codecs(monkeypatch):
    pj = render(monkeypatch, {"external_address": "203.0.113.7", "local_net": ["10.0.0.0/8"], "codecs": "g722,alaw"})["pjsip.conf"]
    assert "external_signaling_address=203.0.113.7" in pj and "local_net=10.0.0.0/8" in pj and "allow=g722,alaw" in pj


def test_render_queues_and_dialplan(monkeypatch):
    out = render(monkeypatch)
    qu, ex = out["queues.conf"], out["extensions.conf"]
    assert "[q3]" in qu and "member => PJSIP/2204,0,2204" in qu and "PJSIP/2205" not in qu
    assert "Queue(q3,t,,,20)" in ex and "Goto(queue-11,s,1)" in ex      # cascade to the next group
    assert "[queue-11]" in ex and "Queue(q11,t)" in ex                    # no next group: keep waiting
    assert "exten => 9992,1,Set(__HOPS=0)" in ex and "exten => 2204,1,Dial(PJSIP/2204,30)" in ex
    assert "exten => 2205" not in ex and "exten => *8,1,Pickup()" in ex
    assert "rtpstart=10000" in out["rtp.conf"]


def test_options_parse():
    db = FakeDB()
    o = Options(db, settings)
    changed, bad = o.parse({"sip_port": "5070", "codecs": "alaw,ulaw", "external_address": "bad host;x",
                            "local_net": "10.0.0.0/8\n192.168.1.0/24", "rtp_start": "10000", "rtp_end": "20000",
                            "language": "ru", "smtp_security": "starttls", "notify_enabled": "on"})
    assert changed["sip_port"] == 5070 and changed["local_net"] == ["10.0.0.0/8", "192.168.1.0/24"]
    assert "External address" in bad
    changed, bad = o.parse({"sip_port": "99999", "language": "de", "rtp_start": "10000", "rtp_end": "10010",
                            "smtp_security": "starttls"})
    assert {"SIP port (UDP)", "Phone language", "Last RTP port"} <= set(bad)


def test_sip_digest_and_names():
    assert directory.sip_digest("2204", "pw") == hashlib.md5(b"2204:asterisk:pw").hexdigest()
    assert directory.clean_name(' a\n"b"  <c> ') == "a b c"
    assert len(directory.new_sip_password()) == 16
    assert directory.clean_tg("user_name") == "@user_name"


def test_legacy_passwords(tmp_path):
    p = tmp_path / "pjsip.conf"
    p.write_text("[2204](endpoint-tpl)\nauth=2204\n[2204](auth-tpl)\nusername=2204\npassword=s3cret\n[2204](aor-tpl)\n"
                 "[9091](auth-tpl)\nusername=9091\npassword=other\n", encoding="utf-8")
    assert provision.legacy_passwords(p) == {"2204": "s3cret", "9091": "other"}
