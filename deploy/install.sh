#!/bin/sh
# boris.gb 2026-10-31 15:05 - install/update Asterisk-Panel on the Asterisk server (Debian 12/13), run as root.
# Idempotent: the first run installs everything, later runs update the code and keep data, passwords and .env.
#   /opt/asterisk-panel/install.sh            — uses /opt/asterisk-panel/secrets.env (DB and AMI passwords)
# Settings with defaults (override in the environment or in .env afterwards): PANEL_HOST (first IP), PANEL_PORT=6443
set -eu
APP=/opt/asterisk-panel
cd "$APP"
[ -f secrets.env ] || { echo "no $APP/secrets.env (DB_ASTERISK_PASSWORD, DB_PANEL_PASSWORD, AMI_SECRET)" >&2; exit 2; }
chmod 600 secrets.env
. ./secrets.env

export DEBIAN_FRONTEND=noninteractive
need=""
for p in postgresql caddy python3-venv; do dpkg -s "$p" >/dev/null 2>&1 || need="$need $p"; done
[ -z "$need" ] || { apt-get update -qq && apt-get install -y -qq $need >/dev/null; }

# --- system user: reads recordings (group asterisk), owns only the data directory ---
id astpanel >/dev/null 2>&1 || useradd -r -d "$APP" -s /usr/sbin/nologin astpanel
getent group asterisk >/dev/null && usermod -aG asterisk astpanel
mkdir -p "$APP/data"
chown -R astpanel:astpanel "$APP/data"
# boris.gb 2026-10-31 15:37 - the panel writes its Asterisk config files here (subscribers, groups, settings);
# Asterisk reads them through its group: setgid keeps new files in group asterisk
if getent group asterisk >/dev/null; then
  mkdir -p /etc/asterisk/panel
  chown astpanel:asterisk /etc/asterisk/panel
  chmod 2750 /etc/asterisk/panel
fi

# --- PostgreSQL: database asterisk; role asterisk writes cdr/cel/queue_log, role panel reads them and owns schema panel ---
psql_su() { su - postgres -c "psql -v ON_ERROR_STOP=1 -qtAX $*"; }
psql_su "-c \"SELECT 1\"" >/dev/null
for r in asterisk panel; do
  [ "$(psql_su "-c \"SELECT 1 FROM pg_roles WHERE rolname='$r'\"")" = 1 ] || psql_su "-c \"CREATE ROLE $r LOGIN\""
done
psql_su "-c \"ALTER ROLE asterisk PASSWORD '$DB_ASTERISK_PASSWORD'\""
psql_su "-c \"ALTER ROLE panel PASSWORD '$DB_PANEL_PASSWORD'\""
[ "$(psql_su "-c \"SELECT 1 FROM pg_database WHERE datname='asterisk'\"")" = 1 ] || psql_su "-c \"CREATE DATABASE asterisk OWNER asterisk ENCODING 'UTF8' TEMPLATE template0\""
PGPASSWORD="$DB_ASTERISK_PASSWORD" psql -h 127.0.0.1 -U asterisk -d asterisk -v ON_ERROR_STOP=1 -qX -f "$APP/deploy/sql/asterisk.sql"
psql_su "-d asterisk -c \"CREATE SCHEMA IF NOT EXISTS panel AUTHORIZATION panel\""
PGPASSWORD="$DB_ASTERISK_PASSWORD" psql -h 127.0.0.1 -U asterisk -d asterisk -v ON_ERROR_STOP=1 -qX \
  -c "GRANT USAGE ON SCHEMA public TO panel; GRANT SELECT ON cdr, cel, queue_log TO panel;" \
  -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO panel;"

# --- Python environment ---
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip >/dev/null
"$APP/venv/bin/pip" install -q -r "$APP/panel/requirements.txt"

# --- .env (created once; secrets from secrets.env are refreshed every run) ---
HOST=${PANEL_HOST:-$(hostname -I | awk '{print $1}')}
PORT=${PANEL_PORT:-6443}
if [ ! -f .env ]; then
  cat > .env <<EOF
# boris.gb $(date '+%Y-%m-%d %H:%M') - Asterisk-Panel settings (mode 600, never commit)
PANEL_HOST=$HOST
PANEL_PORT=$PORT
# internal = certificate from Caddy's own CA; a public domain can use Let's Encrypt: replace with an e-mail
PANEL_TLS=internal
SESSION_SECRET=$("$APP/venv/bin/python" -c "import secrets; print(secrets.token_urlsafe(48))")
COOKIE_SECURE=true
TZ=Europe/Moscow
DEFAULT_LANG=ru
DEFAULT_REGION=RU
SLA_SECONDS=20
PANEL_DATA=$APP/data
IVR_MAP=/etc/asterisk/panel/ivr_map.json
RECORDINGS_DIR=/var/spool/asterisk/monitor
AMI_HOST=127.0.0.1
AMI_PORT=5038
AMI_USER=panel
DATABASE_URL=
AMI_SECRET=
EOF
fi
sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://panel:$DB_PANEL_PASSWORD@127.0.0.1:5432/asterisk|; s|^AMI_SECRET=.*|AMI_SECRET=$AMI_SECRET|" .env
chown root:astpanel .env
chmod 640 .env
. ./.env

# --- Caddy (HTTPS on PANEL_PORT) ---
sed -e "s|__HOST__|$PANEL_HOST|g" -e "s|__PORT__|$PANEL_PORT|g" -e "s|__TLS__|${PANEL_TLS:-internal}|g" \
  "$APP/deploy/Caddyfile.in" > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 || { caddy validate --config /etc/caddy/Caddyfile; exit 1; }

# --- Asterisk config files of the panel (boris.gb 2026-10-31 15:37) ---
# first run: subscribers and groups are taken over from the current pjsip.conf / ivr_map.json; files are written,
# Asterisk starts using them once its main files include them (see deploy/asterisk-examples)
if [ -d /etc/asterisk/panel ]; then
  ( cd "$APP/panel" && set -a && . ../.env && set +a && runuser -u astpanel --preserve-environment -- ../venv/bin/python -m app.provision init )
fi

# --- service ---
install -m 644 "$APP/deploy/asterisk-panel.service" /etc/systemd/system/asterisk-panel.service
systemctl daemon-reload
systemctl enable -q asterisk-panel caddy
systemctl restart asterisk-panel
systemctl reload caddy 2>/dev/null || systemctl restart caddy
sleep 3
curl -fsS http://127.0.0.1:8080/healthz && echo
echo "Panel: https://$PANEL_HOST:$PANEL_PORT"
