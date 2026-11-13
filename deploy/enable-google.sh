#!/usr/bin/env bash
# Switch on "Connect Google Calendar". Safe to re-run.
# Before this, store the OAuth client from Google Cloud Console in SSM:
#   aws ssm put-parameter --name /atya/GOOGLE_CLIENT_ID --type String --value '...'
#   aws ssm put-parameter --name /atya/GOOGLE_CLIENT_SECRET --type SecureString --value '...'
# This creates CONNECTIONS_KEY (encrypts stored Google tokens) if it doesn't exist yet.
# Then: deploy/deploy.sh
set -euo pipefail
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1} AWS_PAGER=""
for p in GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET; do
  aws ssm get-parameter --name "/atya/$p" >/dev/null 2>&1 || { echo "missing /atya/$p in SSM (see the top of this file)"; exit 1; }
done
if aws ssm get-parameter --name /atya/CONNECTIONS_KEY >/dev/null 2>&1; then
  echo "CONNECTIONS_KEY already set (kept: changing it would make stored tokens unreadable)"
else
  KEY=$(openssl rand -base64 32 | tr '+/' '-_')
  aws ssm put-parameter --name /atya/CONNECTIONS_KEY --type SecureString --value "$KEY" >/dev/null
  echo "CONNECTIONS_KEY created"
fi
BASE=$(aws ssm get-parameter --name /atya/WEB_BASE_URL --query Parameter.Value --output text)
echo
echo "Redirect URI to register in Google Cloud Console: $BASE/connect/google/callback"
echo "Now run: deploy/deploy.sh"
