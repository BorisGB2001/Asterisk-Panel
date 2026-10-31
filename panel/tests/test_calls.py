import datetime

from app import calls
from app.ivrmap import IvrMap

T0 = datetime.datetime(2026, 10, 31, 10, 0, 0)
USERS = {"2204": "Оператор 1", "2203": "Оператор 2"}
NAMES = {n: e for e, n in USERS.items()}


def ev(i, sec, etype, chan="PJSIP/trunk-00000001", uid="1.1", **kw):
    row = {"id": i, "eventtype": etype, "eventtime": T0 + datetime.timedelta(seconds=sec), "userdeftype": None,
           "cid_name": "", "cid_num": "79161234567", "cid_dnid": "", "exten": "74951234567", "context": "from-trunk",
           "channame": chan, "appname": "", "appdata": "", "uniqueid": uid, "linkedid": "1.1", "peer": "", "extra": ""}
    row.update(kw)
    return row


def user(i, sec, kind, value):
    # as cel_pgsql writes it into a text eventtype column: TYPE + {"extra": value}
    return ev(i, sec, kind, appname="CELGenUserEvent", extra='{"extra":"%s"}' % value)


def q(i, sec, event, queue="q3", agent="NONE", *data):
    d = list(data) + [""] * 5
    return {"id": i, "time": T0 + datetime.timedelta(seconds=sec), "callid": "1.1", "queuename": queue, "agent": agent,
            "event": event, "data1": d[0], "data2": d[1], "data3": d[2], "data4": d[3], "data5": d[4]}


def ivr_call():
    return [ev(1, 0, "CHAN_START"), ev(2, 0, "ANSWER"), user(3, 0, "LINE", "74951234567"),
            user(4, 0, "RECORDING", "2026/10/31/1.1-79161234567.wav"),
            user(5, 1, "IVR_NODE", "r"), user(6, 4, "IVR_KEY", "r:0"), user(7, 4, "IVR_NODE", "r0"),
            user(8, 20, "IVR_KEY", "r0:4"), user(9, 20, "QUEUE_ENTER", "q3")]


def test_answered_in_queue():
    cel = ivr_call() + [ev(10, 26, "CHAN_START", "PJSIP/2204-00000002", "1.2"),
                        ev(11, 30, "ANSWER", "PJSIP/2204-00000002", "1.2"),
                        ev(12, 30, "BRIDGE_ENTER", "PJSIP/2204-00000002", "1.2"),
                        ev(13, 90, "HANGUP", "PJSIP/trunk-00000001"), ev(14, 90, "LINKEDID_END")]
    ql = [q(1, 20, "ENTERQUEUE", "q3", "NONE", "", "79161234567", "1"),
          q(2, 30, "CONNECT", "q3", "Оператор 1", "10", "1.2", "4"),
          q(3, 90, "COMPLETECALLER", "q3", "Оператор 1", "10", "60", "1")]
    r = calls.summarize(cel, ql, [], USERS, NAMES)
    assert r["status"] == "answered"
    assert r["direction"] == "inbound" and r["source"] == "trunk"
    assert r["ivr_path"] == ["r", "r0"] and r["ivr_keys"] == ["r:0", "r0:4"]
    assert r["lang"] == "ru"
    assert r["queues"] == ["q3"] and r["answered_queue"] == "q3"
    assert r["answered_by"] == "2204" and r["rung"] == ["2204"]
    assert (r["ivr_sec"], r["wait_sec"], r["talk_sec"], r["total_sec"]) == (19, 10, 60, 90)
    assert r["recording"].endswith(".wav")


def test_missed_with_cascade():
    cel = ivr_call() + [user(10, 40, "QUEUE_ENTER", "q11"), ev(11, 55, "HANGUP"), ev(12, 55, "LINKEDID_END")]
    ql = [q(1, 20, "ENTERQUEUE", "q3"), q(2, 40, "RINGNOANSWER", "q3", "Оператор 1", "20000"),
          q(3, 40, "EXITWITHTIMEOUT", "q3", "NONE", "1", "1", "20"), q(4, 40, "ENTERQUEUE", "q11"),
          q(5, 55, "ABANDON", "q11", "NONE", "1", "1", "15")]
    r = calls.summarize(cel, ql, [], USERS, NAMES)
    assert r["status"] == "missed"
    assert r["queues"] == ["q3", "q11"]
    assert r["rings"] == 1
    assert r["wait_sec"] == 35
    assert r["rung"] == ["2204"]


def test_hung_up_in_ivr():
    cel = ivr_call()[:7] + [ev(8, 12, "HANGUP"), ev(9, 12, "LINKEDID_END")]
    r = calls.summarize(cel, [], [], USERS, NAMES)
    assert r["status"] == "ivr" and r["last_node"] == "r0" and r["first_queue"] is None


def test_internal_answered():
    cel = [ev(1, 0, "CHAN_START", "PJSIP/2204-00000001", "2.1", cid_num="2204", exten="2203", context="internal", linkedid="2.1"),
           ev(2, 1, "CHAN_START", "PJSIP/2203-00000002", "2.2", linkedid="2.1"),
           ev(3, 5, "ANSWER", "PJSIP/2203-00000002", "2.2", linkedid="2.1"),
           ev(4, 5, "BRIDGE_ENTER", "PJSIP/2203-00000002", "2.2", linkedid="2.1"),
           ev(5, 35, "LINKEDID_END", "PJSIP/2204-00000001", "2.1", linkedid="2.1")]
    r = calls.summarize(cel, [], [], USERS, NAMES)
    assert r["direction"] == "internal" and r["source"] == "2204"
    assert r["status"] == "answered" and r["answered_by"] == "2203"
    assert (r["wait_sec"], r["talk_sec"]) == (5, 30)
    assert r["rung"] == ["2203"]


def test_user_defined_with_userdeftype():
    r = ev(1, 0, "USER_DEFINED", userdeftype="IVR_NODE", extra="r0")
    assert calls.is_user(r) and calls.user_type(r) == "IVR_NODE" and calls.user_value(r) == "r0"


def test_user_value_json_and_plain():
    assert calls.user_value({"extra": '{"extra":"r03"}'}) == "r03"
    assert calls.user_value({"extra": "r03"}) == "r03"


def test_endpoint():
    assert calls.endpoint("PJSIP/2204-0000000d") == "2204"
    assert calls.endpoint("PJSIP/trunk-provider-0000000d") == "trunk-provider"
    assert calls.endpoint("Local/7410@internal-00000003;2") == "7410@internal"


def test_timeline(tmp_path):
    p = tmp_path / "ivr_map.json"
    p.write_text('{"nodes": {"r": {"title": "Выбор языка", "timeout": 10, "keys": {"0": {"type": "menu", "target": "r0"}}},'
                 ' "r0": {"title": "Главное меню RU", "keys": {}}}, "queues": {"q3": {"name": "Операторы"}},'
                 ' "users": {"2204": "Оператор"}}', encoding="utf-8")
    imap = IvrMap(p)
    cel = ivr_call() + [ev(10, 26, "CHAN_START", "PJSIP/2204-00000002", "1.2"),
                        ev(11, 26, "CHAN_START", "PJSIP/2203-00000003", "1.3"), ev(12, 40, "LINKEDID_END")]
    steps = calls.timeline(cel, [q(1, 20, "ENTERQUEUE", "q3")], imap)
    texts = [s["text"] for s in steps]
    assert any("Выбор языка" in s for s in texts)
    assert any(s["kind"] == "key" and "Главное меню RU" in s["note"] for s in steps)
    rings = [s for s in steps if s["kind"] == "ring"]
    assert len(rings) == 1 and "2204" in rings[0]["who"] and "2203" in rings[0]["who"]
    assert steps[-1]["kind"] == "end" and steps[-1]["rel"] == 40
