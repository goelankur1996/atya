#!/usr/bin/env bash
# Move the REAL bot from v1 (Notion, laptop) to ATYA on AWS.
#
# Before running: stop the v1 bot on your laptop (Telegram allows only one
# program to read a bot's messages; v1 must not process anything after the
# final import below).
#
# What it does:
# 1. Stores the real bot's token (read from the v1 .env) as the server's token.
# 2. On the server: wipes the trial database, runs a fresh, complete Notion
#    import, then starts the bot on the real token.
# Notion is only read. To roll back: stop the server app, restart v1.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
V1_ENV=${V1_ENV:-$HOME/Desktop/AssistantBot/PersonalAssistantBot/.env}

read -r -p "Is the v1 bot stopped on your laptop? Type 'yes' to continue: " ok
[ "$ok" = yes ] || { echo "Stop v1 first."; exit 1; }

TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$V1_ENV" | cut -d= -f2-)
[ -n "$TOKEN" ] || { echo "No TELEGRAM_BOT_TOKEN in $V1_ENV"; exit 1; }
aws ssm put-parameter --name /atya/TELEGRAM_BOT_TOKEN --value "$TOKEN" --type SecureString --overwrite >/dev/null
echo "real bot token stored (${#TOKEN} chars)"

deploy/run.sh "set -euo pipefail
cd /opt/atya
./fetch-env.sh
docker compose down -v
docker compose up -d db
docker compose run --rm app python -m atya.notion_import
docker compose up -d
sleep 5
docker compose logs --tail=20 app"
echo
echo "Cutover done. Send the real bot a capture and a question."
