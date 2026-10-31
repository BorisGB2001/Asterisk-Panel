# Asterisk-Panel

[Русская версия](README.ru.md)

A web panel for Asterisk call statistics in the spirit of [awg-panel](https://github.com/BorisGB2001/awg-panel): every call goes to PostgreSQL, the panel shows reports and the full path of each call through the IVR.

| | |
|---|---|
| Address | `https://<server>:6443` (port is `PANEL_PORT` in `.env`) |
| Install dir | `/opt/asterisk-panel` on the Asterisk server |
| Services | `asterisk-panel` (Python, FastAPI on 127.0.0.1:8080), `caddy` (HTTPS), `postgresql` |
| Data | database `asterisk` in PostgreSQL; `/opt/asterisk-panel/data/geo` — DB-IP databases |
| Login | password + authenticator code (TOTP) |

## Features

- **Overview** — calls, answered, missed in queue, left in the IVR, wait for an operator and SLA, talk time, phones online, active channels right now, chart by hour/day, weekday × hour heatmap, groups, countries, latest calls. Periods: today, yesterday, 7/30 days, this month, last month, custom.
- **Calls** — search by number, name or call id; filters by result, direction, group, country, IVR language, operator, IVR menu; CSV export.
- **Call card** — who called (number, country, region, carrier), how it came in (trunk, or phone and the IP it is registered from), the IVR path with menu titles, keys pressed, groups and transfers between them, who answered, wait and talk, recording player, a **step-by-step timeline** and the **raw call log** (CEL, queue_log, CDR as Asterisk wrote them).
- **Countries** — where calls come from (caller number via libphonenumber), regions and carriers inside a country, where phones are registered from.
- **Groups (queues)** — entered, answered, SLA, passed on to the next group, abandoned, unanswered rings, wait and talk; operators of each group.
- **Operators** — rings offered, answered / not answered, answer rate, talk time, internal calls, whether the phone is online.
- **IVR** — the menu tree with visits, hang-ups, timeouts and how often each key was pressed; language choice.
- **Phones** — SIP registrations: IP, country/city, provider, user agent, RTT; address history, registration log, failed sign-ins (password guessing) with IP and country.
- **Log** — panel sign-ins, account changes, played recordings, exports.
- **Administrators** — super administrators and viewers; temporary password and TOTP setup on the first sign-in.

Light/dark theme, English and Russian interface (add a language with one JSON file).

## How the data is collected

Asterisk writes to PostgreSQL itself, so nothing is lost while the panel is stopped:

| Table | Module | Contents |
|---|---|---|
| `cdr` | `cdr_pgsql` | CDR records + `recordingfile` column (custom CDR variable) |
| `cel` | `cel_pgsql` | all channel events, `DTMF`, and dialplan events from `CELGenUserEvent`: `LINE`, `RECORDING`, `IVR_NODE`, `IVR_KEY` (`node:key`, `node:timeout`, `node:invalid`), `IVR_EXIT`, `QUEUE_ENTER`, `QUEUE_GIVEUP` |
| `queue_log` | `res_config_pgsql` (realtime) | queue events: enter, ring, connect, no answer, abandon, exit with timeout |

The panel (role `panel`, schema `panel`):

- **aggregator** — every 5 s turns each finished call (`LINKEDID_END`) into a row of `panel.calls`: IVR path, language, groups, who answered, durations, country/region of the number, IP of the phone;
- **AMI collector** (127.0.0.1:5038, user `panel`) — phone registrations (`ContactStatus`) and failed sign-ins (security events) → `panel.devices`, `panel.device_log`; IP → country with DB-IP Lite;
- **IVR map** `/etc/asterisk/panel/ivr_map.json` — menu titles, keys, groups and staff (format: [deploy/asterisk-examples/ivr_map.example.json](deploy/asterisk-examples/ivr_map.example.json)).

Examples of the Asterisk side: [pgsql-and-ami.conf](deploy/asterisk-examples/pgsql-and-ami.conf) (CDR/CEL/queue_log to PostgreSQL, AMI user) and [extensions.example.conf](deploy/asterisk-examples/extensions.example.conf) (the CEL events in the dialplan).

## Installation

Requirements: Debian 12/13, Asterisk 20+ built with `cdr_pgsql`, `cel_pgsql` and `res_config_pgsql` (i.e. with `libpq-dev`).

1. Create `secrets.env` on your workstation (keep it out of git):
   ```
   DB_ASTERISK_PASSWORD=<random>
   DB_PANEL_PASSWORD=<random>
   AMI_SECRET=<random>
   ```
2. Install the panel (PostgreSQL, Caddy, venv, database, roles, tables, `.env`, service):
   ```bash
   ./deploy.sh root@<server> secrets.env
   ```
3. Configure Asterisk with the same passwords (see the examples above), put the IVR map into `/etc/asterisk/panel/ivr_map.json`, then `core reload` and `logger reload`.
4. First administrator (the temporary password is printed once):
   ```bash
   ssh root@<server> 'cd /opt/asterisk-panel/panel && set -a && . ../.env && set +a && runuser -u astpanel --preserve-environment -- ../venv/bin/python -m app.admins create admin super'
   ```
   Sign in with the user name, the temporary password and an **empty code** → the panel shows a QR code for the authenticator → then asks for a new password.
5. Certificate: by default `PANEL_TLS=internal` — Caddy issues it from its own CA (works for private addresses). To avoid the browser warning, import `/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt` as trusted on the workstations. For a public domain set `PANEL_HOST` to the domain and `PANEL_TLS` to your e-mail.

Address and port: `PANEL_HOST`, `PANEL_PORT`, `PANEL_TLS` in `/opt/asterisk-panel/.env`, then run `/opt/asterisk-panel/deploy/install.sh`.

To skip the host argument, put it into `.deploy-host` (ignored by git): `echo root@<server> > .deploy-host`.

## Updating

```bash
./deploy.sh root@<server>
```

The code is copied to `/opt/asterisk-panel`, dependencies are updated, the schema is applied again (idempotent) and the service restarts. `.env`, `secrets.env` and the data are kept. Calls in Asterisk are not interrupted.

After a change in the call assembly logic, recompute all calls:

```bash
ssh root@<server> 'cd /opt/asterisk-panel/panel && set -a && . ../.env && set +a && PANEL_COLLECTORS=0 runuser -u astpanel --preserve-environment -- ../venv/bin/python -m app.calls rebuild'
```

## Lost access

```bash
# new temporary password and authenticator setup again
ssh root@<server> 'cd /opt/asterisk-panel/panel && set -a && . ../.env && set +a && runuser -u astpanel --preserve-environment -- ../venv/bin/python -m app.admins reset admin'
```

## Development

```bash
cd panel && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -m pytest -q tests
.venv/bin/python -m app.i18n      # new phrases → locales/en.json, missing translations in other locales
```

## Credits

IP geolocation: [DB-IP Lite](https://db-ip.com) (CC BY 4.0). Phone numbers: [libphonenumber](https://github.com/google/libphonenumber) via [phonenumbers](https://github.com/daviddrysdale/python-phonenumbers).
