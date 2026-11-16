#!/usr/bin/env bash
# Switch on real phone calls (Plivo) on the existing server. Safe to re-run.
# Needs in SSM: /atya/PLIVO_AUTH_ID, /atya/PLIVO_AUTH_TOKEN, /atya/PLIVO_NUMBER (and WEB_DOMAIN already on).
# Installs the current docker-compose.yml + Caddyfile (adds the audio bridge) and restarts.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
step() { printf '\n== %s\n' "$*"; }

step "Plivo settings in SSM (lengths only)"
for name in PLIVO_AUTH_ID PLIVO_AUTH_TOKEN PLIVO_NUMBER WEB_BASE_URL; do
  v=$(aws ssm get-parameter --name "/atya/$name" --with-decryption --query Parameter.Value --output text 2>/dev/null || true)
  [ -n "$v" ] || { echo "missing: /atya/$name"; exit 1; }
  echo "ok: $name (${#v} characters)"
done

step "Install the audio bridge on the server and restart"
b64() { base64 < "$1" | tr -d '\n'; }
deploy/run.sh "set -e; cd /opt/atya
echo $(b64 deploy/server/docker-compose.yml) | base64 -d > docker-compose.yml
echo $(b64 deploy/server/Caddyfile) | base64 -d > Caddyfile
./update.sh
docker compose --profile web restart caddy >/dev/null
sleep 5
docker compose ps --format '{{.Service}}: {{.Status}}'
docker compose logs --tail=5 callbridge"
echo
echo "Done. Send /callme to the bot to test (trial account: your number must be verified in Plivo)."
