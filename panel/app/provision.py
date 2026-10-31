"""Asterisk config files written by the panel into /etc/asterisk/panel/ and applied with a reload over AMI.

The main Asterisk files only include them:
  pjsip.conf      → #include panel/pjsip.conf        transport, templates, one endpoint/auth/aor per subscriber
  queues.conf     → #include panel/queues.conf       one queue q<id> per group
  extensions.conf → #include panel/extensions.conf   queue-<id> contexts (cascade to the next group), [internal-panel]
  rtp.conf        → #include panel/rtp.conf          RTP port range
Calls in progress are not interrupted by a reload. Transport changes (port, external address) need a restart.

CLI (on the server, as the panel user):
  python -m app.provision init      — first run: import subscribers and groups from the current Asterisk config, write files
  python -m app.provision write     — write the files from the database (no reload)
  python -m app.provision apply     — write the files and reload Asterisk
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

from . import directory
from .ami import AMIError

log = logging.getLogger("provision")
FILES = ("pjsip.conf", "queues.conf", "extensions.conf", "rtp.conf")
RELOAD_MODULES = ("res_pjsip.so", "app_queue.so", "pbx_config.so", "res_rtp_asterisk.so")
TRANSPORT_KEYS = ("sip_bind", "sip_port", "external_address", "local_net")


def _head(what: str) -> list[str]:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return [f"; Asterisk-Panel {ts} - {what}", "; Written by the panel from its database: edit in the panel, not here.", ""]


def _safe(v) -> str:
    """Last line of defence: nothing that could start a new line, section or comment in a config file."""
    return re.sub(r"[\x00-\x1f\x7f;\[\]]", "", str(v))


def render(db, opts) -> dict[str, str]:
    o = opts.all()
    subs = [s for s in directory.subscribers(db) if s["sip_enabled"] and s["sip_digest"]]
    groups = directory.groups(db)
    gids = {g["id"] for g in groups}
    enabled = {s["ext"] for s in subs}

    pj = _head("SIP transport and subscribers") + [
        "[transport-udp]", "type=transport", "protocol=udp", f"bind={_safe(o['sip_bind'])}:{int(o['sip_port'])}"]
    if o["external_address"]:
        pj += [f"external_media_address={_safe(o['external_address'])}",
               f"external_signaling_address={_safe(o['external_address'])}"]
        pj += [f"local_net={_safe(n)}" for n in o["local_net"]]
    pj += ["", "[endpoint-tpl](!)", "type=endpoint", "context=internal", "disallow=all", f"allow={_safe(o['codecs'])}",
           "direct_media=no", "rtp_symmetric=yes", "force_rport=yes", "rewrite_contact=yes", f"language={_safe(o['language'])}",
           "call_group=1", "pickup_group=1", f"moh_suggest={_safe(o['moh_hold'])}", "",
           "[aor-tpl](!)", "type=aor", "remove_existing=yes", f"qualify_frequency={int(o['qualify_frequency'])}", "",
           "[auth-tpl](!)", "type=auth", "auth_type=digest", f"realm={directory.REALM}", ""]
    for s in subs:
        ext, name = _safe(s["ext"]), _safe(directory.clean_name(s["name"])) or s["ext"]
        pj += [f"; {name}", f"[{ext}](endpoint-tpl)", f"auth={ext}", f"aors={ext}", f'callerid="{name}" <{ext}>',
               f"[{ext}](auth-tpl)", f"username={ext}", f"password_digest=MD5:{_safe(s['sip_digest'])}",
               f"[{ext}](aor-tpl)", f"max_contacts={int(s['max_contacts'] or o['max_contacts'])}", ""]

    qu = _head("groups (queues)")
    for g in groups:
        qu += [f"[q{g['id']}]", f"; {_safe(directory.clean_name(g['name']))}" + (f" ({_safe(g['number'])})" if g["number"] else ""),
               f"strategy={_safe(g['strategy'])}", f"timeout={int(g['ring_sec'])}", "retry=1", "wrapuptime=0", "ringinuse=no",
               "joinempty=yes", "leavewhenempty=no", "announce-frequency=0", "announce-holdtime=no"]
        # member name = extension: reports map it to the current subscriber name
        qu += [f"member => PJSIP/{_safe(e)},0,{_safe(e)}" for e in g["members"] if e in enabled]
        qu.append("")

    hops, wait, busy = int(o["max_hops"]), _safe(o["moh_wait"]), _safe(o["moh_busy"])
    ex = _head("groups and extensions dialplan")
    for g in groups:
        q = f"q{g['id']}"
        nxt = g["next_id"] if g["next_id"] in gids and g["next_id"] != g["id"] else None
        ex += [f"[queue-{g['id']}]", f"; {_safe(directory.clean_name(g['name']))}: {int(g['ring_sec'])} s, then "
               + (f"group {nxt}" if nxt else "keep waiting"),
               "exten => s,1,Set(HOPS=$[${HOPS}+1])", f" same => n,CELGenUserEvent(QUEUE_ENTER,{q})",
               f" same => n,GotoIf($[${{HOPS}}>{hops}]?giveup)",
               f" same => n,Set(CHANNEL(musicclass)=${{IF($[${{QUEUE_MEMBER({q},free)}}>0]?{wait}:{busy})}})"]
        if nxt:
            ex += [f" same => n,Queue({q},t,,,{int(g['ring_sec'])})", f" same => n,Goto(queue-{nxt},s,1)"]
        else:
            ex += [f" same => n,Queue({q},t)"]
        ex += [f" same => n(giveup),CELGenUserEvent(QUEUE_GIVEUP,{q})", " same => n,Hangup()", ""]
    ex += ["[internal-panel]"]
    for g in groups:
        if g["number"]:
            ex += [f"exten => {_safe(g['number'])},1,Set(__HOPS=0)", f" same => n,Goto(queue-{g['id']},s,1)"]
    for s in subs:
        ex += [f"exten => {_safe(s['ext'])},1,Dial(PJSIP/{_safe(s['ext'])},{int(o['ring_ext_sec'])})", " same => n,Hangup()"]
    ex += [f"exten => {_safe(o['pickup'])},1,Pickup()", ""]

    rtp = _head("RTP ports") + ["[general]", f"rtpstart={int(o['rtp_start'])}", f"rtpend={int(o['rtp_end'])}", ""]
    return {"pjsip.conf": "\n".join(pj), "queues.conf": "\n".join(qu), "extensions.conf": "\n".join(ex),
            "rtp.conf": "\n".join(rtp)}


def write(db, opts, target: Path) -> list[str]:
    """Writes the files atomically; returns the names that changed (comment lines with the time are not compared)."""
    target.mkdir(parents=True, exist_ok=True)
    changed = []
    strip = lambda text: "\n".join(x for x in text.splitlines() if not x.startswith("; Asterisk-Panel "))  # noqa: E731
    for name, text in render(db, opts).items():
        path = target / name
        try:
            if strip(path.read_text(encoding="utf-8")) == strip(text):
                continue
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=target)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
        changed.append(name)
    return changed


def transport_text(target: Path) -> str:
    try:
        text = (target / "pjsip.conf").read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.split("[endpoint-tpl]", 1)[0].split("[transport-udp]", 1)[-1]


def apply(db, opts, ami, target: Path, actor: str = "panel") -> dict:
    """Writes the files and reloads what changed. Result: changed files, reloaded modules, restart needed, error."""
    before = transport_text(target)
    out = {"changed": [], "reloaded": [], "restart": False, "error": None}
    try:
        out["changed"] = write(db, opts, target)
    except OSError as e:
        out["error"] = f"{target}: {e.strerror or e}"
        return out
    out["restart"] = transport_text(target) != before and bool(before)
    mods = {"pjsip.conf": "res_pjsip.so", "queues.conf": "app_queue.so", "extensions.conf": "pbx_config.so",
            "rtp.conf": "res_rtp_asterisk.so"}
    for name in out["changed"]:
        try:
            ami.reload(mods[name])
            out["reloaded"].append(mods[name])
        except (OSError, AMIError) as e:
            out["error"] = f"AMI: {e}"
    db.event("asterisk_apply", {k: v for k, v in out.items() if v}, actor=actor)
    return out


# ---- first run: take subscribers and groups over from the config made by the old generator ----

AUTH_RE = re.compile(r"^\[(?P<ext>[0-9]{2,8})\]\(auth-tpl\)\s*$")


def legacy_passwords(pjsip: Path) -> dict[str, str]:
    """[2204](auth-tpl) ... password=... in the old pjsip.conf → {ext: password}."""
    out, cur = {}, None
    try:
        lines = pjsip.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        m = AUTH_RE.match(line.strip())
        if m:
            cur = m.group("ext")
            continue
        if line.startswith("["):
            cur = None
        elif cur and line.strip().startswith("password="):
            out[cur] = line.strip().split("=", 1)[1]
    return out


def import_legacy(db, imap, pjsip: Path) -> tuple[int, int]:
    """Subscribers and groups from ivr_map.json + SIP passwords from pjsip.conf; only into empty tables."""
    if db.val("SELECT count(*) FROM subscribers") or db.val("SELECT count(*) FROM groups"):
        return 0, 0
    pw = legacy_passwords(pjsip)
    data = imap.data()
    users = data.get("users", {})
    for ext, name in users.items():
        if not directory.EXT_RE.match(ext):
            continue
        digest = directory.sip_digest(ext, pw[ext]) if ext in pw else None
        db.run("INSERT INTO subscribers (ext, name, sip_digest, sip_enabled) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
               (ext, directory.clean_name(name) or ext, digest, digest is not None))
    queues = data.get("queues", {})
    for q, info in queues.items():
        gid = int(q.lstrip("q"))
        nxt = int(info["next"].lstrip("q")) if info.get("next") and info["next"] != q else None
        db.run("INSERT INTO groups (id, name, number, strategy, ring_sec, next_id) VALUES (%s,%s,%s,%s,%s,%s) "
               "ON CONFLICT DO NOTHING", (gid, directory.clean_name(info.get("name", q), 80), info.get("number") or None,
                                          info.get("strategy", "ringall"), int(info.get("wait") or 20), nxt))
        for pos, ext in enumerate(info.get("members", []), 1):
            db.run("INSERT INTO group_members (group_id, ext, pos) SELECT %s, %s, %s WHERE EXISTS "
                   "(SELECT 1 FROM subscribers WHERE ext = %s) ON CONFLICT DO NOTHING", (gid, ext, pos, ext))
    return len(users), len(queues)


def _cli() -> None:
    from .ami import AMI
    from .config import settings
    from .db import DB
    from .ivrmap import IvrMap
    from .options import Options

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("init", "write", "apply"):
        sys.exit(__doc__)
    db = DB(settings.database_url, size=1)
    db.migrate()
    opts = Options(db, settings)
    target = settings.asterisk_dir
    if cmd == "init":
        imap = IvrMap(settings.ivr_map)
        subs, grps = import_legacy(db, imap, settings.asterisk_etc / "pjsip.conf")
        if subs or grps:
            db.event("asterisk_import", {"subscribers": subs, "groups": grps}, actor="cli")
            print(f"imported {subs} subscribers, {grps} groups")
        print("written:", ", ".join(write(db, opts, target)) or "nothing changed")
    elif cmd == "write":
        print("written:", ", ".join(write(db, opts, target)) or "nothing changed")
    else:
        print(apply(db, opts, AMI(settings), target, actor="cli"))
    db.close()


if __name__ == "__main__":
    _cli()
