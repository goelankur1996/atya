#!/usr/bin/env bash
# Switch on the web view (HTTPS) for the existing server. Safe to re-run.
#   deploy/enable-web.sh                 use <ip>.sslip.io (no DNS setup needed)
#   deploy/enable-web.sh atya.example.com  use your own domain (point an A record
#                                          at the server's IP first; printed below)
#
# 1. Gives the server a fixed public IP (Elastic IP), so the address survives restarts.
# 2. Opens ports 80 and 443 (80 only redirects to HTTPS and answers certificate checks).
# 3. Stores WEB_DOMAIN / WEB_BASE_URL in SSM, installs the Caddy (HTTPS) setup on
#    the server and restarts. Caddy gets a free Let's Encrypt certificate itself.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
step() { printf '\n== %s\n' "$*"; }

ID=$(aws ec2 describe-instances --filters Name=tag:Name,Values=atya Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
[ "$ID" != None ] || { echo "no running server tagged Name=atya"; exit 1; }

step "Fixed IP address"
ALLOC=$(aws ec2 describe-addresses --filters Name=tag:Name,Values=atya --query 'Addresses[0].AllocationId' --output text)
if [ "$ALLOC" = None ]; then
  ALLOC=$(aws ec2 allocate-address --domain vpc \
    --tag-specifications 'ResourceType=elastic-ip,Tags=[{Key=Name,Value=atya}]' --query AllocationId --output text)
fi
aws ec2 associate-address --instance-id "$ID" --allocation-id "$ALLOC" --allow-reassociation >/dev/null
IP=$(aws ec2 describe-addresses --allocation-ids "$ALLOC" --query 'Addresses[0].PublicIp' --output text)
echo "ok: $IP"

step "Open ports 80 and 443"
SG=$(aws ec2 describe-instances --instance-ids "$ID" \
  --query 'Reservations[0].Instances[0].SecurityGroups[0].GroupId' --output text)
for port in 80 443; do
  aws ec2 authorize-security-group-ingress --group-id "$SG" --ip-permissions \
    "IpProtocol=tcp,FromPort=$port,ToPort=$port,IpRanges=[{CidrIp=0.0.0.0/0,Description=ATYA web}]" \
    >/dev/null 2>&1 || true   # already open
done
echo "ok: $SG"

DOMAIN=${1:-$(echo "$IP" | tr . -).sslip.io}
step "Domain: $DOMAIN"
if [ -n "${1:-}" ]; then
  RESOLVED=$(dig +short "$DOMAIN" A 2>/dev/null | tail -1 || true)
  if [ "$RESOLVED" != "$IP" ]; then
    echo "$DOMAIN points to '${RESOLVED:-nothing}', not $IP."
    echo "Add an A record: $DOMAIN -> $IP, wait a few minutes, then run this again."
    exit 1
  fi
fi
aws ssm put-parameter --name /atya/WEB_DOMAIN --value "$DOMAIN" --type SecureString --overwrite >/dev/null
aws ssm put-parameter --name /atya/WEB_BASE_URL --value "https://$DOMAIN" --type SecureString --overwrite >/dev/null
echo "ok: stored in SSM"

step "Install HTTPS setup on the server and restart"
b64() { base64 < "$1" | tr -d '\n'; }
deploy/run.sh "set -e; cd /opt/atya
echo $(b64 deploy/server/docker-compose.yml) | base64 -d > docker-compose.yml
echo $(b64 deploy/server/Caddyfile) | base64 -d > Caddyfile
echo $(b64 deploy/server/update.sh) | base64 -d > update.sh
chmod 700 update.sh
./update.sh
docker compose ps"

step "Waiting for the certificate (up to 2 min)"
for i in $(seq 1 24); do
  if curl -fsS -o /dev/null "https://$DOMAIN/"; then
    echo "ok: https://$DOMAIN is up. Send /web to the bot to sign in."
    exit 0
  fi
  sleep 5
done
echo "Not reachable yet. Check: deploy/run.sh \"cd /opt/atya && docker compose logs --tail=40 caddy\""
exit 1
