"""Calls: assemble one call (linkedid) from Asterisk's cel + queue_log + cdr rows.

summarize() → one row of panel.calls (for reports); timeline() → the step-by-step path for the call page.
Aggregator — background thread that turns every finished call (CEL LINKEDID_END) into a panel.calls row.

CLI: python -m app.calls rebuild   — recompute all rows (after a change in the logic)
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import sys
import threading

from . import phone
from .fmt import N_
from .i18n import t

log = logging.getLogger("calls")
UTC = datetime.timezone.utc
CHAN_RE = re.compile(r"^(?P<tech>[A-Za-z0-9]+)/(?P<name>.+?)(?:-[0-9a-f]{8}(?:;\d)?)?$")


def endpoint(channame: str | None) -> str | None:
    """PJSIP/2204-0000000d → 2204; Local/7410@internal-00000003;2 → 7410@internal."""
    if not channame:
        return None
    m = CHAN_RE.match(channame)
    return m.group("name") if m else channame


def is_local(channame: str | None) -> bool:
    return bool(channame) and channame.startswith("Local/")


def utc(ts: datetime.datetime | None) -> datetime.datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def user_value(row: dict) -> str:
    """Value of CELGenUserEvent(TYPE,value): cel.extra is either the plain value or JSON {"extra": value}."""
    extra = (row.get("extra") or "").strip()
    if extra.startswith("{"):
        try:
            d = json.loads(extra)
            v = d.get("extra", "")
            return v if isinstance(v, str) else json.dumps(v)
        except ValueError:
            pass
    return extra


# CELGenUserEvent(TYPE,…) from the dialplan. cel_pgsql writes TYPE straight into the text column eventtype
# (with an integer eventtype column it would be USER_DEFINED + userdeftype) — both are understood.
USER_TYPES = {"LINE", "RECORDING", "IVR_NODE", "IVR_KEY", "IVR_EXIT", "QUEUE_ENTER", "QUEUE_GIVEUP"}


def is_user(row: dict) -> bool:
    return row["eventtype"] == "USER_DEFINED" or row["eventtype"] in USER_TYPES


def user_type(row: dict) -> str:
    return row.get("userdeftype") or (row["eventtype"] if row["eventtype"] != "USER_DEFINED" else "")


def _int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def summarize(cel: list[dict], qlog: list[dict], cdr: list[dict], users: dict, name_to_ext: dict) -> dict:
    """One call → dict for panel.calls (without phone/device enrichment)."""
    cel = sorted(cel, key=lambda r: r["id"])
    qlog = sorted(qlog, key=lambda r: (r["time"] or datetime.datetime.min, r["id"]))
    starts = [r for r in cel if r["eventtype"] == "CHAN_START"]
    first = starts[0] if starts else cel[0]
    orig_uid = first["uniqueid"]
    orig_chan = first["channame"] or ""
    ep = endpoint(orig_chan)
    if is_local(orig_chan):
        source = "local"
    elif ep in users:
        source = ep
    else:
        source = "trunk"

    uev = [(utc(r["eventtime"]), user_type(r), user_value(r)) for r in cel if is_user(r)]
    start = utc(min(r["eventtime"] for r in cel))
    ends = [r for r in cel if r["eventtype"] == "LINKEDID_END"]
    end = utc(ends[-1]["eventtime"]) if ends else utc(max(r["eventtime"] for r in cel))

    line = next((v for _, k, v in uev if k == "LINE"), None)
    ivr_path: list[str] = []
    for _, k, v in uev:
        if k == "IVR_NODE" and (not ivr_path or ivr_path[-1] != v):
            ivr_path.append(v)
    ivr_keys = [v for _, k, v in uev if k == "IVR_KEY"]
    lang = None
    for v in ivr_keys:
        if v.startswith("r:"):
            lang = {"r:0": "ru", "r:9": "en"}.get(v)
            break
    q_enter = [(ts, v) for ts, k, v in uev if k == "QUEUE_ENTER"]
    queues: list[str] = []
    for _, v in q_enter:
        if v not in queues:
            queues.append(v)
    for r in qlog:
        if r["event"] == "ENTERQUEUE" and r["queuename"] and r["queuename"] not in queues:
            queues.append(r["queuename"])
    recording = next((v for _, k, v in uev if k == "RECORDING"), None) or \
        next((r.get("recordingfile") for r in cdr if r.get("recordingfile")), None)

    connect = next((r for r in qlog if r["event"] == "CONNECT"), None)
    rings = sum(1 for r in qlog if r["event"] == "RINGNOANSWER")
    t_ivr = next((ts for ts, k, _ in uev if k == "IVR_NODE"), None)
    t_q = q_enter[0][0] if q_enter else utc(next((r["time"] for r in qlog if r["event"] == "ENTERQUEUE"), None))

    answered_by = answered_queue = None
    ivr_sec = wait_sec = talk_sec = 0
    if t_ivr:
        ivr_sec = int(((t_q or end) - t_ivr).total_seconds())
    if connect:
        status = "answered"
        answered_queue = connect["queuename"]
        answered_by = name_to_ext.get(connect["agent"]) or connect["agent"]
        t_conn = utc(connect["time"])
        wait_sec = int((t_conn - t_q).total_seconds()) if t_q else _int(connect["data1"])
        done = next((r for r in qlog if r["event"] in ("COMPLETECALLER", "COMPLETEAGENT")), None)
        talk_sec = _int(done["data2"]) if done else int((end - t_conn).total_seconds())
    elif t_q:
        status = "missed"
        wait_sec = int((end - t_q).total_seconds())
    elif t_ivr:
        status = "ivr"
    else:
        # internal call without IVR/queue: answered if another channel answered and joined the bridge
        other = [r for r in cel if r["uniqueid"] != orig_uid and r["eventtype"] in ("ANSWER", "BRIDGE_ENTER")
                 and not is_local(r["channame"])]
        if other:
            status = "answered"
            answered_by = endpoint(other[0]["channame"])
            t_ans = utc(other[0]["eventtime"])
            wait_sec = int((t_ans - start).total_seconds())
            talk_sec = int((end - t_ans).total_seconds())
        else:
            status = "unanswered"
            wait_sec = int((end - start).total_seconds())

    return {
        "linkedid": cel[0]["linkedid"],
        "start_at": start,
        "end_at": end,
        "direction": "inbound" if (line or ivr_path) else "internal",
        "source": source,
        "caller_num": first["cid_num"] or "",
        "caller_name": first["cid_name"] or "",
        "did": line,
        "dst": first["exten"] or "",
        "lang": lang,
        "status": status,
        "answered_by": answered_by,
        "answered_queue": answered_queue,
        "first_queue": queues[0] if queues else None,
        "queues": queues,
        "ivr_path": ivr_path,
        "ivr_keys": ivr_keys,
        "last_node": ivr_path[-1] if ivr_path else None,
        "ivr_sec": max(ivr_sec, 0),
        "wait_sec": max(wait_sec, 0),
        "talk_sec": max(talk_sec, 0),
        "total_sec": max(int((end - start).total_seconds()), 0),
        "rings": rings,
        "recording": recording,
        "uniqueids": sorted({r["uniqueid"] for r in cel if r["uniqueid"]}),
    }


# ---- timeline for the call page ----

QLOG_TEXT = {
    "ENTERQUEUE": N_("entered the queue"),
    "RINGNOANSWER": N_("did not answer"),
    "RINGCANCELED": N_("ringing cancelled"),
    "CONNECT": N_("answered"),
    "COMPLETECALLER": N_("caller hung up"),
    "COMPLETEAGENT": N_("operator hung up"),
    "ABANDON": N_("caller hung up while waiting"),
    "EXITWITHTIMEOUT": N_("nobody answered — next group"),
    "EXITEMPTY": N_("queue empty"),
    "TRANSFER": N_("transferred"),
    "BLINDTRANSFER": N_("transferred"),
    "ATTENDEDTRANSFER": N_("transferred"),
}


def timeline(cel: list[dict], qlog: list[dict], imap) -> list[dict]:
    """Steps of the call in time order: {'ts', 'kind', 'text', 'who', 'note'}; ringing of several phones merged."""
    cel = sorted(cel, key=lambda r: r["id"])
    starts = [r for r in cel if r["eventtype"] == "CHAN_START"]
    orig_uid = (starts[0] if starts else cel[0])["uniqueid"] if cel else None
    talked = {r["uniqueid"] for r in cel if r["eventtype"] == "BRIDGE_ENTER"}
    items: list[dict] = []

    def add(ts, kind, text, who="", note=""):
        items.append({"ts": utc(ts), "kind": kind, "text": text, "who": who, "note": note})

    for r in cel:
        ev, chan = r["eventtype"], r["channame"]
        ep = endpoint(chan)
        who = imap.user_title(ep) if ep in imap.users else (ep or "")
        orig = r["uniqueid"] == orig_uid
        # Local channels (test calls, internal hops): keep only the caller's own channel and the call end
        if is_local(chan) and not is_user(r) and not orig and ev != "LINKEDID_END":
            continue
        if ev == "CHAN_START":
            if orig:
                add(r["eventtime"], "start", t("Call from {num} to {dst}", num=r["cid_num"] or "?", dst=r["exten"] or "?"),
                    who, r["cid_name"] or "")
            else:
                add(r["eventtime"], "ring", t("Ringing"), who)
        elif ev == "ANSWER":
            add(r["eventtime"], "answer", t("Answered by the line") if orig else t("Picked up"), who)
        elif ev == "BRIDGE_ENTER" and not orig:
            add(r["eventtime"], "bridge", t("Conversation started"), who)
        elif ev == "HANGUP":
            note = ""
            try:
                d = json.loads(r["extra"] or "{}")
                note = t("cause {n}", n=d.get("hangupcause")) if d.get("hangupcause") is not None else ""
            except ValueError:
                pass
            if orig or r["uniqueid"] in talked:
                add(r["eventtime"], "hangup", t("Hung up"), who, note)
        elif ev == "LINKEDID_END":
            add(r["eventtime"], "end", t("Call ended"))
        elif is_user(r):
            k, v = user_type(r), user_value(r)
            if k == "IVR_NODE":
                add(r["eventtime"], "ivr", t("IVR menu: {title}", title=imap.node_title(v)), note=v)
            elif k == "IVR_KEY":
                node, _, rest = v.partition(":")
                if rest == "timeout":
                    to = imap.nodes.get(node, {}).get("timeout")
                    add(r["eventtime"], "key", t("No key pressed") + (f" ({to} s)" if to else ""), note=imap.node_title(node))
                elif rest.startswith("invalid"):
                    add(r["eventtime"], "key", t("Invalid key {key}", key=rest.partition(":")[2] or "?"),
                        note=t("menu repeated"))
                else:
                    add(r["eventtime"], "key", t("Key {key}", key=rest), note=imap.key_text(node, rest))
            elif k == "IVR_EXIT":
                add(r["eventtime"], "ivr", t("Exit from IVR to the default queue"))
            elif k == "QUEUE_ENTER":
                add(r["eventtime"], "queue", t("Group: {name}", name=imap.queue_title(v)), note=v)
            elif k == "QUEUE_GIVEUP":
                add(r["eventtime"], "queue", t("Too many group transfers, call dropped"), note=v)
            elif k == "RECORDING":
                add(r["eventtime"], "rec", t("Recording started"), note=v)
            elif k == "LINE":
                add(r["eventtime"], "line", t("Incoming line {did}", did=v))

    for r in qlog:
        ev = r["event"]
        if ev not in QLOG_TEXT:
            continue
        agent = r["agent"] if r["agent"] and r["agent"] != "NONE" else ""
        ext = imap.ext_by_name(agent)
        who = imap.user_title(ext) if ext else agent
        note = ""
        if ev == "ENTERQUEUE":
            note = imap.queue_title(r["queuename"])
        elif ev == "RINGNOANSWER":
            note = t("{n} s ringing", n=_int(r["data1"]) // 1000)
        elif ev == "CONNECT":
            note = t("waited {n} s", n=_int(r["data1"]))
        elif ev in ("COMPLETECALLER", "COMPLETEAGENT"):
            note = t("talk {n} s", n=_int(r["data2"]))
        elif ev in ("ABANDON", "EXITWITHTIMEOUT"):
            note = t("waited {n} s", n=_int(r["data3"])) + " · " + imap.queue_title(r["queuename"])
        add(r["time"], "q-" + ev.lower(), t(QLOG_TEXT[ev]), who, note)

    items.sort(key=lambda i: i["ts"] or datetime.datetime.min.replace(tzinfo=UTC))
    # several phones ringing at once (ringall) → one line
    merged: list[dict] = []
    for it in items:
        prev = merged[-1] if merged else None
        if it["kind"] == "ring" and prev and prev["kind"] == "ring" and abs((it["ts"] - prev["ts"]).total_seconds()) <= 1:
            prev["who"] += ", " + it["who"]
            continue
        merged.append(it)
    t0 = merged[0]["ts"] if merged else None
    for it in merged:
        it["rel"] = int((it["ts"] - t0).total_seconds()) if t0 and it["ts"] else 0
    return merged


# ---- aggregator ----

PENDING_SQL = """
SELECT c.linkedid FROM cel c
WHERE c.eventtype = 'LINKEDID_END' AND c.eventtime < (now() AT TIME ZONE 'UTC') - interval '3 seconds'
  AND NOT EXISTS (SELECT 1 FROM panel.calls k WHERE k.linkedid = c.linkedid)
ORDER BY c.id LIMIT 200
"""

UPSERT_COLS = ("linkedid", "start_at", "end_at", "direction", "source", "caller_num", "caller_name", "caller_e164",
               "caller_country", "caller_region", "caller_carrier", "device_ip", "device_country", "did", "dst", "lang",
               "status", "answered_by", "answered_queue", "first_queue", "queues", "ivr_path", "ivr_keys", "last_node",
               "ivr_sec", "wait_sec", "talk_sec", "total_sec", "rings", "recording", "uniqueids")


def raw(db, linkedid: str) -> tuple[list[dict], list[dict], list[dict]]:
    cel = db.all("SELECT * FROM cel WHERE linkedid = %s ORDER BY id", (linkedid,))
    uids = sorted({r["uniqueid"] for r in cel if r["uniqueid"]} | {linkedid})
    qlog = db.all("SELECT * FROM queue_log WHERE callid = ANY(%s) ORDER BY time, id", (uids,))
    cdr = db.all("SELECT * FROM cdr WHERE linkedid = %s ORDER BY id", (linkedid,))
    return cel, qlog, cdr


def build_row(db, linkedid: str, imap, settings) -> dict | None:
    cel, qlog, cdr = raw(db, linkedid)
    if not cel:
        return None
    users = imap.users
    name_to_ext = {n: e for e, n in users.items()}
    row = summarize(cel, qlog, cdr, users, name_to_ext)
    info = phone.lookup(row["caller_num"], settings.default_region, frozenset(users))
    row.update(caller_e164=info["e164"], caller_country=info["country"], caller_region=info["region"],
               caller_carrier=info["carrier"], device_ip=None, device_country=None)
    if row["source"] in users:
        dev = db.one("SELECT ip, country FROM device_log WHERE endpoint = %s AND kind IN ('register', 'ip_change') "
                     "AND ts <= %s ORDER BY ts DESC LIMIT 1", (row["source"], row["start_at"])) or \
            db.one("SELECT ip, country FROM devices WHERE endpoint = %s", (row["source"],))
        if dev:
            row.update(device_ip=dev["ip"], device_country=dev["country"])
    return row


def save(db, row: dict) -> None:
    cols = ", ".join(UPSERT_COLS)
    ph = ", ".join(["%s"] * len(UPSERT_COLS))
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in UPSERT_COLS if c != "linkedid")
    db.run(f"INSERT INTO calls ({cols}) VALUES ({ph}) ON CONFLICT (linkedid) DO UPDATE SET {upd}",
           tuple(row[c] for c in UPSERT_COLS))


class Aggregator:
    def __init__(self, db, imap, settings):
        self.db, self.imap, self.settings = db, imap, settings

    def step(self) -> int:
        n = 0
        for r in self.db.all(PENDING_SQL):
            try:
                row = build_row(self.db, r["linkedid"], self.imap, self.settings)
                if row:
                    save(self.db, row)
                    n += 1
            except Exception:  # noqa: BLE001 — one broken call must not stop the others
                log.exception("call %s", r["linkedid"])
        return n

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.step()
            except Exception:  # noqa: BLE001 — database restart etc.: try again later
                log.exception("aggregator")
            stop.wait(5)


def _cli() -> None:
    from .config import settings
    from .db import DB
    from .ivrmap import IvrMap

    if sys.argv[1:] != ["rebuild"]:
        sys.exit(__doc__)
    db = DB(settings.database_url)
    db.migrate()
    imap = IvrMap(settings.ivr_map)
    ids = [r["linkedid"] for r in db.all("SELECT DISTINCT linkedid FROM cel WHERE eventtype = 'LINKEDID_END'")]
    for lid in ids:
        row = build_row(db, lid, imap, settings)
        if row:
            save(db, row)
    print(f"rebuilt {len(ids)} calls")


if __name__ == "__main__":
    _cli()
