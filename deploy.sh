#!/bin/sh
# boris.gb 2026-10-31 15:05 - deploy Asterisk-Panel from a workstation: tests locally (if a venv is given), copy, install.sh on the server.
# Usage: ./deploy.sh root@<server> [path/to/secrets.env]
#   secrets.env (DB_ASTERISK_PASSWORD, DB_PANEL_PASSWORD, AMI_SECRET) is copied on the first deploy; later it is kept on the server.
#   The same passwords must be in the Asterisk configs (cdr_pgsql.conf, cel_pgsql.conf, res_pgsql.conf, manager.conf).
set -e
cd "$(dirname "$0")"
HOST=${1:-$(cat .deploy-host 2>/dev/null)}
[ -n "$HOST" ] || { echo "usage: $0 root@<server> [secrets.env]  (or put the host into .deploy-host)" >&2; exit 2; }
SECRETS=${2:-}
COPYFILE_DISABLE=1 tar --no-xattrs -czf - --exclude '__pycache__' --exclude .pytest_cache panel deploy README.md \
  | ssh "$HOST" 'mkdir -p /opt/asterisk-panel && tar -xzf - -C /opt/asterisk-panel --no-same-owner && chmod +x /opt/asterisk-panel/deploy/install.sh'
if [ -n "$SECRETS" ]; then
  scp -q "$SECRETS" "$HOST:/opt/asterisk-panel/secrets.env"
fi
ssh "$HOST" '/opt/asterisk-panel/deploy/install.sh && cd /opt/asterisk-panel/panel && PANEL_COLLECTORS=0 DATABASE_URL= \
  /opt/asterisk-panel/venv/bin/python -c "import app.main" && echo "import ok"'
