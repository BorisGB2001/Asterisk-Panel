"""Asterisk panel web app: sign-in, reports, call path through the IVR, phones, administrators."""
from __future__ import annotations

import csv
import io
import logging
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import admins, auth, calls, charts, fmt, i18n, phone, reports
from .ami import AMI, AMIError, Collector
from .config import settings
from .db import DB
from .fmt import N_
from .geo import Geo
from .i18n import t
from .ivrmap import IvrMap

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
fmt.register(templates.env)
limiter = auth.RateLimiter()
log = logging.getLogger("panel")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app.state.db = DB(settings.database_url)
    app.state.db.migrate()
    app.state.imap = IvrMap(settings.ivr_map)
    app.state.geo = Geo(settings.data_dir / "geo")
    app.state.ami = AMI(settings)
    app.state.stop = threading.Event()
    app.state.collector = None
    templates.env.globals.update(GEO_DATE=app.state.geo.built, imap=app.state.imap)
    if settings.collectors:
        threading.Thread(target=app.state.geo.updater, args=(app.state.stop,), name="geo", daemon=True).start()
        app.state.collector = Collector(settings, app.state.db, app.state.geo)
        threading.Thread(target=app.state.collector.run, args=(app.state.stop,), name="ami", daemon=True).start()
        agg = calls.Aggregator(app.state.db, app.state.imap, settings)
        threading.Thread(target=agg.run, args=(app.state.stop,), name="calls", daemon=True).start()
    yield
    app.state.stop.set()
    app.state.db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


# ---- session, CSRF, rendering ----

def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def current_admin(request: Request):
    if "admin" in request.state._state:
        return request.state.admin
    s, row = request.session, None
    if s.get("aid") and s.get("ua") == auth.ua_fingerprint(request.headers.get("user-agent", "")) and \
            time.time() - s.get("at", 0) <= settings.session_hours * 3600:
        row = admins.get(request.app.state.db, s["aid"])
        if row is None or row["disabled"] or row["session_ver"] != s.get("sv"):
            row = None
    if row is None and s.get("aid"):
        s.clear()
    request.state.admin = row
    return row


def require_super(request: Request) -> None:
    row = current_admin(request)
    if not row or row["role"] != "super":
        raise HTTPException(403, t("Only a super administrator can do this."))


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = request.session["csrf"] = auth.new_csrf()
    return tok


def check_csrf(request: Request, token: str) -> None:
    if not secrets.compare_digest(token or "", request.session.get("csrf", "")):
        raise HTTPException(403, "CSRF")
    origin = request.headers.get("origin") or ""
    if origin == "null":
        origin = ""
    origin = origin or request.headers.get("referer") or ""
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "Origin")


THEMES = ("auto", "light", "dark")


def theme_of(request: Request) -> str:
    v = request.cookies.get("theme", "")
    return v if v in THEMES else "auto"


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    row = current_admin(request)
    ctx.setdefault("user", row["username"] if row else None)
    ctx.setdefault("admin", row)
    ctx.setdefault("flash", request.session.pop("flash", None))
    ctx.setdefault("lang", i18n.get())
    ctx.setdefault("theme", theme_of(request))
    ctx.setdefault("path", request.url.path)
    return templates.TemplateResponse(request, name, {"csrf": csrf_token(request), **ctx})


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session["flash"] = {"text": text, "kind": kind}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    i18n.set_lang(request.cookies.get("lang"))
    if path.startswith(("/static/", "/lang/", "/theme/")) or path in ("/healthz", "/login", "/login/totp"):
        return await call_next(request)
    row = current_admin(request)
    if row:
        if row["must_change"] and path not in ("/account", "/logout"):
            return RedirectResponse("/account", status_code=303)
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


app.add_middleware(SessionMiddleware, secret_key=settings.session_secret or secrets.token_urlsafe(48),
                   session_cookie="astp", max_age=settings.session_hours * 3600, same_site="strict",
                   https_only=settings.cookie_secure)


def local_path(p: str) -> bool:
    return p.startswith("/") and not p.startswith("//") and "\\" not in p


def back_to(request: Request) -> RedirectResponse:
    ref = urlsplit(request.headers.get("referer") or "")
    back = (ref.path or "/") + (f"?{ref.query}" if ref.query else "")
    if ref.netloc and ref.netloc != request.headers.get("host", ""):
        back = "/"
    return RedirectResponse(back if local_path(back) else "/", status_code=303)


@app.get("/theme/{mode}")
def set_theme(request: Request, mode: str):
    resp = back_to(request)
    resp.set_cookie("theme", mode if mode in THEMES else "auto", max_age=365 * 86400, httponly=True,
                    secure=settings.cookie_secure, samesite="lax")
    return resp


@app.get("/lang/{code}")
def set_language(request: Request, code: str):
    resp = back_to(request)
    resp.set_cookie("lang", i18n.normalize(code), max_age=365 * 86400, httponly=True, secure=settings.cookie_secure,
                    samesite="lax")
    return resp


@app.get("/healthz")
def healthz(request: Request):
    db_ok = True
    try:
        request.app.state.db.val("SELECT 1")
    except Exception:  # noqa: BLE001
        db_ok = False
    col = request.app.state.collector
    return {"ok": db_ok, "db": db_ok, "ami": bool(col and col.connected)}


# ---- sign-in ----

ENROLL: dict[str, tuple[int, str, float]] = {}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_admin(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", problems=settings.problems(), error=None)


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(""), password: str = Form(""), code: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    ip, db = client_ip(request), request.app.state.db
    wait = limiter.blocked(ip)
    if wait:
        return render(request, "login.html", problems=[], error=t("Too many attempts. Try again in {n} min.", n=wait // 60 + 1))
    row = admins.by_name(db, username)
    ok = admins.check_password(row, password)
    enroll = ok and not row["totp_secret"]
    ok = ok and (enroll or admins.check_totp(db, row, code))
    if not ok:
        limiter.fail(ip)
        db.event("admin_login_failed", {"username": username[:64]}, actor_ip=ip)
        return render(request, "login.html", problems=settings.problems(), error=t("Wrong username, password or code."))
    limiter.success(ip)
    request.session.clear()
    if enroll:
        tok = secrets.token_urlsafe(24)
        ENROLL[tok] = (row["id"], admins.new_totp_secret(), time.time() + 600)
        request.session.update(enroll=tok, csrf=auth.new_csrf())
        return RedirectResponse("/login/totp", status_code=303)
    start_session(request, row, ip)
    return RedirectResponse("/", status_code=303)


def start_session(request: Request, row, ip: str) -> None:
    request.session.clear()
    request.session.update(aid=row["id"], sv=row["session_ver"], ua=auth.ua_fingerprint(request.headers.get("user-agent", "")),
                           at=int(time.time()), csrf=auth.new_csrf())
    admins.logged_in(request.app.state.db, row, ip)
    request.app.state.db.event("admin_login", actor=row["username"], actor_ip=ip)


def enrollment(request: Request):
    for k, v in list(ENROLL.items()):
        if v[2] < time.time():
            ENROLL.pop(k, None)
    tok = request.session.get("enroll", "")
    item = ENROLL.get(tok)
    if not item:
        return None, None, None
    return tok, admins.get(request.app.state.db, item[0]), item[1]


def qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


@app.get("/login/totp", response_class=HTMLResponse)
def totp_form(request: Request):
    tok, row, secret = enrollment(request)
    if not row:
        return RedirectResponse("/login", status_code=303)
    return render(request, "totp_setup.html", username=row["username"], secret=secret, error=None,
                  qr=qr_svg(admins.provisioning_uri(row["username"], secret)))


@app.post("/login/totp", response_class=HTMLResponse)
def totp_bind(request: Request, code: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    ip, db = client_ip(request), request.app.state.db
    tok, row, secret = enrollment(request)
    if not row or row["disabled"] or limiter.blocked(ip):
        return RedirectResponse("/login", status_code=303)
    if not admins.bind_totp(db, row, secret, code):
        limiter.fail(ip)
        return render(request, "totp_setup.html", username=row["username"], secret=secret,
                      error=t("Wrong code. Check the time on the phone and try again."),
                      qr=qr_svg(admins.provisioning_uri(row["username"], secret)))
    ENROLL.pop(tok, None)
    db.event("acct_totp_bound", actor=row["username"], actor_ip=ip)
    start_session(request, admins.get(db, row["id"]), ip)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    check_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    return render(request, "account.html", error=None)


@app.post("/account", response_class=HTMLResponse)
def account_save(request: Request, old: str = Form(""), new: str = Form(""), new2: str = Form(""), csrf: str = Form("")):
    check_csrf(request, csrf)
    row, db = current_admin(request), request.app.state.db
    if not admins.check_password(row, old):
        return render(request, "account.html", error=t("The current password is wrong."))
    if len(new) < 12 or new != new2:
        return render(request, "account.html", error=t("The new password must be at least 12 characters and match the repeat."))
    admins.set_password(db, row["id"], new)
    db.event("acct_password", actor=row["username"], actor_ip=client_ip(request))
    start_session(request, admins.get(db, row["id"]), client_ip(request))
    flash(request, t("Password changed."))
    return RedirectResponse("/", status_code=303)


# ---- reports ----

def period_of(request: Request) -> reports.Period:
    q = request.query_params
    return reports.period(q.get("p", "today"), q.get("from", ""), q.get("to", ""))


FILTER_KEYS = ("status", "direction", "country", "queue", "agent", "node", "lang", "q")


def filters_of(request: Request) -> dict:
    return {k: request.query_params.get(k, "").strip()[:64] for k in FILTER_KEYS if request.query_params.get(k, "").strip()}


def live_channels(request: Request) -> tuple[list[dict], str | None]:
    try:
        rows = request.app.state.ami.channels()
    except (OSError, AMIError) as e:
        return [], str(e)
    out = []
    for r in rows:
        ep = calls.endpoint(r.get("Channel"))
        out.append({"channel": r.get("Channel"), "ep": ep, "caller": r.get("CallerIDNum"), "name": r.get("CallerIDName"),
                    "context": r.get("Context"), "exten": r.get("Exten"), "state": r.get("ChannelStateDesc"),
                    "app": r.get("Application"), "data": r.get("ApplicationData"), "dur": r.get("Duration"),
                    "linkedid": r.get("Linkedid"), "bridged": r.get("BridgeId")})
    return out, None


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    db, p, f = request.app.state.db, period_of(request), filters_of(request)
    k = reports.kpis(db, p, f, settings.sla_seconds)
    live, live_err = live_channels(request)
    devs = db.all("SELECT status, count(*) n FROM devices GROUP BY 1")
    online = sum(r["n"] for r in devs if r["status"] in ("Reachable", "Created", "NonQualified", "Updated"))
    chart = charts.bars(reports.series(db, p, f), [t("answered"), t("missed"), t("left in IVR / not answered")])
    top_c = reports.countries(db, p, f)[:10]
    queues = reports.queues(db, p, settings.sla_seconds)
    recent, _ = reports.calls(db, p, f, limit=15)
    return render(request, "index.html", period=p, k=k, live=live, live_err=live_err, online=online,
                  devices_total=sum(r["n"] for r in devs), chart=chart, top_countries=top_c, queues=queues,
                  recent=recent, heat=charts.heatmap(reports.heat(db, p, f)), filters=f)


@app.get("/calls", response_class=HTMLResponse)
def calls_page(request: Request, page: int = 1):
    db, p, f = request.app.state.db, period_of(request), filters_of(request)
    page = max(page, 1)
    rows, total = reports.calls(db, p, f, limit=100, offset=(page - 1) * 100)
    return render(request, "calls.html", period=p, rows=rows, total=total, page=page, pages=(total + 99) // 100,
                  filters=f, countries=reports.countries(db, p), agents=reports.agents(db, p))


CSV_COLS = ("start_at", "end_at", "direction", "status", "caller_num", "caller_name", "caller_e164", "caller_country",
            "caller_region", "caller_carrier", "source", "device_ip", "device_country", "did", "dst", "lang", "ivr_path",
            "ivr_keys", "last_node", "queues", "answered_queue", "answered_by", "ivr_sec", "wait_sec", "talk_sec",
            "total_sec", "rings", "recording", "linkedid")


@app.get("/calls.csv")
def calls_csv(request: Request):
    db, p, f = request.app.state.db, period_of(request), filters_of(request)
    rows, _ = reports.calls(db, p, f, limit=100000)
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(CSV_COLS)
    for r in rows:
        w.writerow([fmt.dt(r[c]) if c in ("start_at", "end_at") else
                    (" > ".join(r[c]) if isinstance(r[c], list) else ("" if r[c] is None else r[c])) for c in CSV_COLS])
    db.event("calls_exported", {"period": p.label, "rows": len(rows), "filters": f}, actor=current_admin(request)["username"],
             actor_ip=client_ip(request))
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="calls-{p.since:%Y%m%d}.csv"'})


LINKEDID = re.compile(r"^[0-9.]{6,40}$")


def call_or_404(request: Request, linkedid: str):
    if not LINKEDID.match(linkedid):
        raise HTTPException(404)
    db = request.app.state.db
    row = db.one("SELECT * FROM calls WHERE linkedid = %s", (linkedid,))
    cel, qlog, cdr = calls.raw(db, linkedid)
    if row is None and not cel:
        raise HTTPException(404, t("Not found"))
    if row is None:   # still in progress or not aggregated yet — build on the fly
        imap = request.app.state.imap
        row = calls.summarize(cel, qlog, cdr, imap.users, {n: e for e, n in imap.users.items()})
        info = phone.lookup(row["caller_num"], settings.default_region, frozenset(imap.users))
        row.update(caller_e164=info["e164"], caller_country=info["country"], caller_region=info["region"],
                   caller_carrier=info["carrier"], device_ip=None, device_country=None)
        row["in_progress"] = not any(r["eventtype"] == "LINKEDID_END" for r in cel)
    return row, cel, qlog, cdr


def recording_path(rel: str | None) -> Path | None:
    if not rel:
        return None
    base = settings.recordings_dir.resolve()
    p = (base / rel).resolve()
    return p if p.is_relative_to(base) and p.is_file() else None


@app.get("/call/{linkedid}", response_class=HTMLResponse)
def call_page(request: Request, linkedid: str):
    row, cel, qlog, cdr = call_or_404(request, linkedid)
    imap = request.app.state.imap
    steps = calls.timeline(cel, qlog, imap)
    rec = recording_path(row.get("recording"))
    return render(request, "call.html", c=row, steps=steps, cel=cel, qlog=qlog, cdr=cdr, has_rec=bool(rec),
                  pretty=phone.pretty)


@app.get("/call/{linkedid}/recording")
def call_recording(request: Request, linkedid: str, download: int = 0):
    row, *_ = call_or_404(request, linkedid)
    rec = recording_path(row.get("recording"))
    if not rec:
        raise HTTPException(404, t("No recording"))
    request.app.state.db.event("recording_played", {"linkedid": linkedid}, actor=current_admin(request)["username"],
                               actor_ip=client_ip(request))
    return FileResponse(rec, media_type="audio/wav", filename=rec.name if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/countries", response_class=HTMLResponse)
def countries_page(request: Request, cc: str = ""):
    db, p = request.app.state.db, period_of(request)
    rows = reports.countries(db, p)
    cc = cc.upper()[:2] if cc and cc != "--" else cc
    regions = reports.regions(db, p, cc) if cc else []
    chart = charts.hbars([(f"{fmt.flag(r['cc'])} {fmt.country(r['cc'])}", r["calls"], f"/countries?{p.query()}&cc={r['cc']}")
                          for r in rows[:15]])
    return render(request, "countries.html", period=p, rows=rows, cc=cc, regions=regions, chart=chart,
                  devices=reports.device_countries(db, p))


@app.get("/queues", response_class=HTMLResponse)
def queues_page(request: Request, q: str = ""):
    db, p = request.app.state.db, period_of(request)
    rows = reports.queues(db, p, settings.sla_seconds)
    known = request.app.state.imap.queues
    seen = {r["q"] for r in rows}
    for name in known:
        if name not in seen:
            rows.append({"q": name, "entered": 0, "answered": 0, "in_sla": 0, "abandoned": 0, "passed_on": 0,
                         "no_answer": 0, "avg_wait": 0, "max_wait_answered": 0, "avg_abandon_wait": 0, "avg_talk": 0,
                         "sum_talk": 0})
    agents = reports.agents(db, p, q) if q else []
    return render(request, "queues.html", period=p, rows=rows, q=q, agents=agents, sla=settings.sla_seconds)


@app.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request):
    db, p = request.app.state.db, period_of(request)
    imap = request.app.state.imap
    rows = reports.agents(db, p)
    direct = reports.direct_answers(db, p)
    devs = {r["endpoint"]: r for r in db.all("SELECT * FROM devices")}
    by_ext = {}
    for r in rows:
        ext = imap.ext_by_name(r["agent"]) or r["agent"]
        by_ext[ext] = {**r, "ext": ext}
    for ext in list(imap.users) + list(direct):
        by_ext.setdefault(ext, {"ext": ext, "agent": imap.users.get(ext, ext), "offered": 0, "answered": 0, "missed": 0,
                                "talk": 0, "avg_talk": 0, "avg_ring": 0, "agent_hangups": 0})
    for ext, d in direct.items():
        by_ext[ext]["direct"] = d["answered"]
        by_ext[ext]["direct_talk"] = d["talk"]
    member_of = {}
    for qn, info in imap.queues.items():
        for ext in info.get("members", []):
            member_of.setdefault(ext, []).append(qn)
    out = sorted(by_ext.values(), key=lambda r: (-(r["answered"] + r.get("direct", 0)), r["ext"]))
    return render(request, "agents.html", period=p, rows=out, devs=devs, member_of=member_of)


@app.get("/ivr", response_class=HTMLResponse)
def ivr_page(request: Request):
    db, p = request.app.state.db, period_of(request)
    imap = request.app.state.imap
    nodes = {r["node"]: r for r in reports.ivr_nodes(db, p)}
    keys = {r["k"]: r["n"] for r in reports.ivr_keys(db, p)}
    tree = []

    def walk(path, depth, seen):
        info = imap.nodes.get(path)
        if not info or path in seen:
            return
        st = nodes.get(path, {})
        tree.append({"path": path, "depth": depth, "info": info, "visits": st.get("visits", 0),
                     "hung_up": st.get("hung_up_here", 0), "timeouts": keys.get(f"{path}:timeout", 0),
                     "key_rows": [(k, a, keys.get(f"{path}:{k}", 0)) for k, a in info.get("keys", {}).items()]})
        for k, a in info.get("keys", {}).items():
            if a.get("type") == "menu":
                walk(a["target"], depth + 1, seen | {path})

    walk("r", 0, set())
    chart = charts.hbars([(imap.node_title(r["node"]), r["visits"], f"/calls?{p.query()}&node={r['node']}")
                          for r in nodes.values()][:20])
    return render(request, "ivr.html", period=p, tree=tree, chart=chart, langs=reports.langs(db, p))


@app.get("/devices", response_class=HTMLResponse)
def devices_page(request: Request, days: int = 7):
    db = request.app.state.db
    imap = request.app.state.imap
    devs = {r["endpoint"]: r for r in db.all("SELECT * FROM devices")}
    rows = [{"ext": e, "name": n, "dev": devs.get(e)} for e, n in sorted(imap.users.items())]
    rows += [{"ext": e, "name": "", "dev": d} for e, d in devs.items() if e not in imap.users]
    days = min(max(days, 1), 365)
    logrows = db.all("SELECT * FROM device_log WHERE ts >= now() - %s * interval '1 day' ORDER BY ts DESC LIMIT 500", (days,))
    fails = db.all("""SELECT ip, coalesce(country, '') cc, coalesce(org, '') org, count(*) n, max(ts) last,
                             array_agg(DISTINCT endpoint) FILTER (WHERE endpoint IS NOT NULL) accounts
                      FROM device_log WHERE kind = 'auth_failed' AND ts >= now() - %s * interval '1 day'
                      GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 50""", (days,))
    countries = db.all("""SELECT coalesce(country, '') cc, count(DISTINCT endpoint) phones, count(*) events
                          FROM device_log WHERE kind IN ('register', 'ip_change') AND ts >= now() - %s * interval '1 day'
                          GROUP BY 1 ORDER BY phones DESC""", (days,))
    return render(request, "devices.html", rows=rows, log=logrows, fails=fails, countries=countries, days=days,
                  ami_ok=bool(request.app.state.collector and request.app.state.collector.connected))


@app.get("/device/{ext}", response_class=HTMLResponse)
def device_page(request: Request, ext: str):
    if not re.match(r"^[\w.@-]{1,64}$", ext):
        raise HTTPException(404)
    db, p = request.app.state.db, period_of(request)
    dev = db.one("SELECT * FROM devices WHERE endpoint = %s", (ext,))
    logrows = db.all("SELECT * FROM device_log WHERE endpoint = %s ORDER BY ts DESC LIMIT 300", (ext,))
    ips = db.all("""SELECT ip, coalesce(country, '') cc, city, org, min(ts) first, max(ts) last, count(*) n
                    FROM device_log WHERE endpoint = %s AND ip IS NOT NULL AND kind IN ('register', 'ip_change')
                    GROUP BY 1, 2, 3, 4 ORDER BY last DESC""", (ext,))
    made, _ = reports.calls(db, p, {"q": ext}, limit=50)
    return render(request, "device.html", ext=ext, dev=dev, log=logrows, ips=ips, period=p, calls_rows=made)


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request, days: int = 30):
    days = min(max(days, 1), 365)
    rows = request.app.state.db.all("SELECT * FROM events WHERE ts >= now() - %s * interval '1 day' ORDER BY ts DESC LIMIT 1000",
                                    (days,))
    return render(request, "events.html", rows=rows, days=days)


@app.get("/api/live")
def api_live(request: Request):
    live, err = live_channels(request)
    return {"channels": live, "error": err}


# ---- administrators ----

@app.get("/admins", response_class=HTMLResponse)
def admins_page(request: Request):
    require_super(request)
    return render(request, "admins.html", rows=admins.all_admins(request.app.state.db), new_pw=request.session.pop("new_pw", None))


@app.post("/admins/add")
def admins_add(request: Request, username: str = Form(""), role: str = Form("viewer"), csrf: str = Form("")):
    check_csrf(request, csrf)
    require_super(request)
    db, me = request.app.state.db, current_admin(request)
    try:
        pw = admins.create(db, username, role)
    except ValueError:
        flash(request, t("Invalid user name or role."), "error")
        return RedirectResponse("/admins", status_code=303)
    except Exception:  # noqa: BLE001 — unique violation
        flash(request, t("This user already exists."), "error")
        return RedirectResponse("/admins", status_code=303)
    db.event("acct_create", {"username": username, "role": role}, actor=me["username"], actor_ip=client_ip(request))
    request.session["new_pw"] = {"username": username, "password": pw}
    return RedirectResponse("/admins", status_code=303)


@app.post("/admins/{admin_id}/{action}")
def admins_action(request: Request, admin_id: int, action: str, csrf: str = Form("")):
    check_csrf(request, csrf)
    require_super(request)
    db, me = request.app.state.db, current_admin(request)
    row = admins.get(db, admin_id)
    if not row or row["id"] == me["id"] or action not in ("reset", "disable", "enable"):
        raise HTTPException(400)
    if action == "reset":
        request.session["new_pw"] = {"username": row["username"], "password": admins.reset(db, admin_id)}
    else:
        db.run("UPDATE admins SET disabled = %s, session_ver = session_ver + 1 WHERE id = %s", (action == "disable", admin_id))
    db.event("acct_" + action, {"username": row["username"]}, actor=me["username"], actor_ip=client_ip(request))
    return RedirectResponse("/admins", status_code=303)


MENU = [("/", N_("Overview"), ()), ("/calls", N_("Calls"), ("/call/",)), ("/countries", N_("Countries"), ()),
        ("/queues", N_("Groups"), ()), ("/agents", N_("Operators"), ()), ("/ivr", N_("IVR"), ()),
        ("/devices", N_("Phones"), ("/device/",)), ("/events", N_("Log"), ())]
templates.env.globals["MENU"] = MENU


def qs(request: Request, drop: str = "", **set_) -> str:
    """Query string of the current page with some parameters removed (drop="p,from") or replaced (key=value)."""
    q = {k: v for k, v in request.query_params.items() if k not in drop.split(",") and k not in set_}
    q.update({k: v for k, v in set_.items() if v not in (None, "")})
    return urlencode(q)


templates.env.globals["qs"] = qs
