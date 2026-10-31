"""Report queries: periods, KPIs, series, countries, queues (groups), operators, IVR usage, call search."""
from __future__ import annotations

import datetime
from dataclasses import dataclass

from .fmt import TZ, N_
from .i18n import t

UTC = datetime.timezone.utc
PERIODS = {"today": N_("today"), "yesterday": N_("yesterday"), "7d": N_("7 days"), "30d": N_("30 days"),
           "month": N_("this month"), "prev": N_("last month"), "90d": N_("90 days")}


@dataclass
class Period:
    key: str
    since: datetime.datetime      # aware, UTC
    until: datetime.datetime
    bucket: str                   # hour | day
    label: str

    @property
    def naive(self) -> tuple[datetime.datetime, datetime.datetime]:
        """For Asterisk tables (timestamp without time zone, UTC)."""
        return self.since.astimezone(UTC).replace(tzinfo=None), self.until.astimezone(UTC).replace(tzinfo=None)

    def query(self) -> str:
        if self.key == "custom":
            return f"p=custom&from={self.since.astimezone(TZ):%Y-%m-%d}&to={(self.until.astimezone(TZ) - datetime.timedelta(days=1)):%Y-%m-%d}"
        return f"p={self.key}"


def period(key: str = "today", date_from: str = "", date_to: str = "", now: datetime.datetime | None = None) -> Period:
    now = (now or datetime.datetime.now(UTC)).astimezone(TZ)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "custom":
        try:
            a = datetime.datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=TZ)
            b = datetime.datetime.strptime(date_to or date_from, "%Y-%m-%d").replace(tzinfo=TZ) + datetime.timedelta(days=1)
            if b <= a:
                a, b = b - datetime.timedelta(days=1), a + datetime.timedelta(days=1)
            label = f"{a:%d.%m.%Y} — {b - datetime.timedelta(days=1):%d.%m.%Y}"
            return Period("custom", a, b, "hour" if (b - a).days <= 2 else "day", label)
        except ValueError:
            key = "today"
    if key == "yesterday":
        a, b = day - datetime.timedelta(days=1), day
    elif key in ("7d", "30d", "90d"):
        a, b = day - datetime.timedelta(days=int(key[:-1]) - 1), day + datetime.timedelta(days=1)
    elif key == "month":
        a, b = day.replace(day=1), day + datetime.timedelta(days=1)
    elif key == "prev":
        b = day.replace(day=1)
        a = (b - datetime.timedelta(days=1)).replace(day=1)
    else:
        key, a, b = "today", day, day + datetime.timedelta(days=1)
    return Period(key, a, b, "hour" if (b - a).days <= 2 else "day", t(PERIODS[key]))


# ---- filters on panel.calls ----

def where(p: Period, f: dict | None = None) -> tuple[str, list]:
    f = f or {}
    sql, args = ["start_at >= %s AND start_at < %s"], [p.since, p.until]
    if f.get("status"):
        sql.append("status = %s")
        args.append(f["status"])
    if f.get("direction"):
        sql.append("direction = %s")
        args.append(f["direction"])
    if f.get("country"):
        sql.append("caller_country = %s")
        args.append(f["country"])
    if f.get("queue"):
        sql.append("%s = ANY(queues)")
        args.append(f["queue"])
    if f.get("agent"):
        sql.append("answered_by = %s")
        args.append(f["agent"])
    if f.get("me"):          # personal cabinet: calls the subscriber took part in
        sql.append("(answered_by = %s OR source = %s OR dst = %s OR %s = ANY(rung))")
        args += [f["me"]] * 4
    if f.get("node"):
        sql.append("%s = ANY(ivr_path)")
        args.append(f["node"])
    if f.get("lang"):
        sql.append("lang = %s")
        args.append(f["lang"])
    if f.get("q"):
        q = "%" + f["q"].replace("%", "").replace("_", "") + "%"
        sql.append("(caller_num ILIKE %s OR caller_e164 ILIKE %s OR caller_name ILIKE %s OR dst ILIKE %s OR linkedid = %s)")
        args += [q, q, q, q, f["q"]]
    return " AND ".join(sql), args


def kpis(db, p: Period, f: dict | None, sla: int) -> dict:
    w, a = where(p, f)
    return db.one(f"""
        SELECT count(*) total,
               count(*) FILTER (WHERE status = 'answered') answered,
               count(*) FILTER (WHERE status = 'missed') missed,
               count(*) FILTER (WHERE status = 'ivr') ivr,
               count(*) FILTER (WHERE status = 'unanswered') unanswered,
               count(*) FILTER (WHERE direction = 'inbound') inbound,
               count(*) FILTER (WHERE first_queue IS NOT NULL) queued,
               count(*) FILTER (WHERE status = 'answered' AND first_queue IS NOT NULL AND wait_sec <= %s) in_sla,
               coalesce(avg(wait_sec) FILTER (WHERE status = 'answered' AND first_queue IS NOT NULL), 0)::int avg_wait,
               coalesce(max(wait_sec) FILTER (WHERE first_queue IS NOT NULL), 0) max_wait,
               coalesce(avg(talk_sec) FILTER (WHERE status = 'answered'), 0)::int avg_talk,
               coalesce(sum(talk_sec), 0) sum_talk,
               coalesce(avg(ivr_sec) FILTER (WHERE ivr_sec > 0), 0)::int avg_ivr,
               coalesce(sum(total_sec), 0) sum_total,
               count(DISTINCT caller_num) callers
        FROM calls WHERE {w}""", (sla, *a))


def series(db, p: Period, f: dict | None = None) -> list[tuple[str, list[float]]]:
    w, a = where(p, f)
    rows = db.all(f"""
        SELECT date_trunc(%s, start_at AT TIME ZONE %s) b,
               count(*) FILTER (WHERE status = 'answered') ans,
               count(*) FILTER (WHERE status = 'missed') mis,
               count(*) FILTER (WHERE status IN ('ivr', 'unanswered')) other
        FROM calls WHERE {w} GROUP BY 1""", (p.bucket, str(TZ), *a))
    return _fill(p, {r["b"]: [r["ans"], r["mis"], r["other"]] for r in rows})


def _fill(p: Period, got: dict) -> list[tuple[str, list[float]]]:
    out = []
    cur = p.since.astimezone(TZ).replace(tzinfo=None)
    end = p.until.astimezone(TZ).replace(tzinfo=None)
    step = datetime.timedelta(hours=1) if p.bucket == "hour" else datetime.timedelta(days=1)
    while cur < end:
        out.append((cur.strftime("%H:%M" if p.bucket == "hour" else "%d.%m"), got.get(cur, [0, 0, 0])))
        cur += step
    return out


def heat(db, p: Period, f: dict | None = None) -> dict[tuple[int, int], float]:
    w, a = where(p, f)
    rows = db.all(f"""SELECT extract(isodow FROM start_at AT TIME ZONE %s)::int d,
                             extract(hour FROM start_at AT TIME ZONE %s)::int h, count(*) n
                      FROM calls WHERE {w} GROUP BY 1, 2""", (str(TZ), str(TZ), *a))
    return {(r["d"] - 1, r["h"]): r["n"] for r in rows}


def countries(db, p: Period, f: dict | None = None) -> list[dict]:
    w, a = where(p, f)
    return db.all(f"""
        SELECT coalesce(caller_country, '') cc, count(*) calls,
               count(*) FILTER (WHERE status = 'answered') answered,
               count(*) FILTER (WHERE status = 'missed') missed,
               count(*) FILTER (WHERE status = 'ivr') ivr,
               coalesce(avg(wait_sec) FILTER (WHERE status = 'answered' AND first_queue IS NOT NULL), 0)::int avg_wait,
               coalesce(sum(talk_sec), 0) talk, count(DISTINCT caller_num) callers
        FROM calls WHERE {w} GROUP BY 1 ORDER BY calls DESC""", a)


def regions(db, p: Period, cc: str) -> list[dict]:
    w, a = where(p, {"country": cc})
    return db.all(f"""
        SELECT coalesce(caller_region, '') region, coalesce(caller_carrier, '') carrier, count(*) calls,
               count(*) FILTER (WHERE status = 'answered') answered,
               count(*) FILTER (WHERE status = 'missed') missed
        FROM calls WHERE {w} GROUP BY 1, 2 ORDER BY calls DESC LIMIT 200""", a)


def device_countries(db, p: Period) -> list[dict]:
    """Where internal callers' phones were registered from (by call)."""
    w, a = where(p)
    return db.all(f"""SELECT coalesce(device_country, '') cc, count(*) calls, count(DISTINCT source) phones
                      FROM calls WHERE {w} AND device_ip IS NOT NULL GROUP BY 1 ORDER BY calls DESC""", a)


QUEUE_SQL = """
WITH ql AS (SELECT * FROM queue_log WHERE time >= %s AND time < %s AND queuename IS NOT NULL AND queuename <> 'NONE')
SELECT queuename q,
       count(*) FILTER (WHERE event = 'ENTERQUEUE') entered,
       count(*) FILTER (WHERE event = 'CONNECT') answered,
       count(*) FILTER (WHERE event = 'CONNECT' AND nullif(data1, '')::int <= %s) in_sla,
       count(*) FILTER (WHERE event = 'ABANDON') abandoned,
       count(*) FILTER (WHERE event = 'EXITWITHTIMEOUT') passed_on,
       count(*) FILTER (WHERE event = 'RINGNOANSWER') no_answer,
       coalesce(avg(nullif(data1, '')::int) FILTER (WHERE event = 'CONNECT'), 0)::int avg_wait,
       coalesce(max(nullif(data1, '')::int) FILTER (WHERE event = 'CONNECT'), 0) max_wait_answered,
       coalesce(avg(nullif(data3, '')::int) FILTER (WHERE event = 'ABANDON'), 0)::int avg_abandon_wait,
       coalesce(avg(nullif(data2, '')::int) FILTER (WHERE event IN ('COMPLETECALLER', 'COMPLETEAGENT')), 0)::int avg_talk,
       coalesce(sum(nullif(data2, '')::int) FILTER (WHERE event IN ('COMPLETECALLER', 'COMPLETEAGENT')), 0) sum_talk
FROM ql GROUP BY 1 ORDER BY entered DESC
"""


def queues(db, p: Period, sla: int) -> list[dict]:
    a, b = p.naive
    return db.all(QUEUE_SQL, (a, b, sla))


AGENT_SQL = """
WITH ql AS (SELECT * FROM queue_log WHERE time >= %s AND time < %s AND agent IS NOT NULL AND agent <> 'NONE'
            AND (%s = '' OR queuename = %s))
SELECT agent,
       count(*) FILTER (WHERE event IN ('CONNECT', 'RINGNOANSWER')) offered,
       count(*) FILTER (WHERE event = 'CONNECT') answered,
       count(*) FILTER (WHERE event = 'RINGNOANSWER') missed,
       coalesce(sum(nullif(data2, '')::int) FILTER (WHERE event IN ('COMPLETECALLER', 'COMPLETEAGENT')), 0) talk,
       coalesce(avg(nullif(data2, '')::int) FILTER (WHERE event IN ('COMPLETECALLER', 'COMPLETEAGENT')), 0)::int avg_talk,
       coalesce(avg(nullif(data3, '')::int) FILTER (WHERE event = 'CONNECT'), 0)::int avg_ring,
       count(*) FILTER (WHERE event = 'COMPLETEAGENT') agent_hangups
FROM ql GROUP BY 1 ORDER BY answered DESC, offered DESC
"""


def agents(db, p: Period, queue: str = "") -> list[dict]:
    a, b = p.naive
    return db.all(AGENT_SQL, (a, b, queue, queue))


def direct_answers(db, p: Period) -> dict[str, dict]:
    """Calls answered by an extension without a queue (internal calls), per extension."""
    w, a = where(p)
    rows = db.all(f"""SELECT answered_by ext, count(*) answered, coalesce(sum(talk_sec), 0) talk FROM calls
                      WHERE {w} AND status = 'answered' AND first_queue IS NULL GROUP BY 1""", a)
    return {r["ext"]: r for r in rows}


def ivr_nodes(db, p: Period) -> list[dict]:
    w, a = where(p)
    return db.all(f"""
        WITH v AS (SELECT linkedid, status, last_node, unnest(ivr_path) node FROM calls WHERE {w})
        SELECT node, count(DISTINCT linkedid) visits,
               count(DISTINCT linkedid) FILTER (WHERE status = 'ivr' AND last_node = node) hung_up_here
        FROM v GROUP BY 1 ORDER BY visits DESC""", a)


def ivr_keys(db, p: Period) -> list[dict]:
    w, a = where(p)
    return db.all(f"""SELECT k, count(*) n FROM (SELECT unnest(ivr_keys) k FROM calls WHERE {w}) x
                      GROUP BY 1 ORDER BY n DESC""", a)


def langs(db, p: Period) -> list[dict]:
    w, a = where(p)
    return db.all(f"""SELECT coalesce(lang, '') lang, count(*) n FROM calls WHERE {w} AND direction = 'inbound'
                      GROUP BY 1 ORDER BY n DESC""", a)


def calls(db, p: Period, f: dict, limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
    w, a = where(p, f)
    total = db.val(f"SELECT count(*) FROM calls WHERE {w}", a)
    rows = db.all(f"SELECT * FROM calls WHERE {w} ORDER BY start_at DESC LIMIT %s OFFSET %s", (*a, limit, offset))
    return rows, total


def sub_kpis(db, p: Period, ext: str) -> dict:
    """Personal cabinet: what happened to the subscriber's calls in the period."""
    w, a = where(p, {"me": ext})
    return db.one(f"""
        SELECT count(*) total,
               count(*) FILTER (WHERE answered_by = %s) answered,
               count(*) FILTER (WHERE %s = ANY(rung) AND answered_by IS DISTINCT FROM %s AND source IS DISTINCT FROM %s) rung_not_me,
               count(*) FILTER (WHERE %s = ANY(rung) AND status <> 'answered' AND source IS DISTINCT FROM %s) missed,
               count(*) FILTER (WHERE source = %s) outgoing,
               count(*) FILTER (WHERE source = %s AND status = 'answered') outgoing_answered,
               coalesce(sum(talk_sec) FILTER (WHERE answered_by = %s OR (source = %s AND status = 'answered')), 0) talk,
               coalesce(avg(talk_sec) FILTER (WHERE answered_by = %s), 0)::int avg_talk,
               coalesce(avg(wait_sec) FILTER (WHERE answered_by = %s AND first_queue IS NOT NULL), 0)::int avg_wait
        FROM calls WHERE {w}""", (ext, ext, ext, ext, ext, ext, ext, ext, ext, ext, ext, ext, *a))


def sub_series(db, p: Period, ext: str) -> list[tuple[str, list[float]]]:
    """Per hour/day: answered by the subscriber, missed (rang the subscriber, nobody answered), outgoing."""
    w, a = where(p, {"me": ext})
    rows = db.all(f"""
        SELECT date_trunc(%s, start_at AT TIME ZONE %s) b,
               count(*) FILTER (WHERE answered_by = %s) s1,
               count(*) FILTER (WHERE %s = ANY(rung) AND status <> 'answered' AND source IS DISTINCT FROM %s) s2,
               count(*) FILTER (WHERE source = %s) s3
        FROM calls WHERE {w} GROUP BY 1""", (p.bucket, str(TZ), ext, ext, ext, ext, *a))
    return _fill(p, {r["b"]: [r["s1"], r["s2"], r["s3"]] for r in rows})
