# Deploying ATYA to AWS

One small server (EC2 t4g.small, arm64, us-east-1) runs Postgres + the app in
Docker, plus Caddy for HTTPS once the web view is on. There is **no SSH**: the
server is managed through AWS Systems Manager with `deploy/run.sh`. The only
open ports are 80/443 for the web view (none before `enable-web.sh`). Secrets live in SSM Parameter Store
under `/atya/`. Nightly database backups go to `s3://atya-backups-<account>/backups/`
(kept 30 days).

All commands run from the repo on your Mac, with the AWS CLI logged in.

| Step | Command |
| --- | --- |
| First-time setup (safe to re-run) | `deploy/setup.sh` |
| Ship new code | `git pull && deploy/deploy.sh` |
| Logs | `deploy/run.sh "cd /opt/atya && docker compose logs --tail=80 app"` |
| Status | `deploy/run.sh "cd /opt/atya && docker compose ps"` |
| Backup now | `deploy/run.sh /opt/atya/backup.sh` |
| Move the real bot from v1 | `deploy/cutover.sh` |
| Switch on the web view (HTTPS) | `deploy/enable-web.sh` (or `deploy/enable-web.sh your.domain`) |
| Health check (read-only) | `deploy/run.sh "$(cat deploy/diagnose.sh)"` |
| Change a secret | `aws ssm put-parameter --name /atya/<NAME> --value '...' --type SecureString --overwrite`, then `deploy/deploy.sh` |

`setup.sh` copies the bot token, owner ID, Anthropic and Voyage keys from your
local `.env`, and the Notion token from the v1 `.env`, into SSM.

Only one program may read a Telegram bot at a time. Stop the local copy
(`docker compose down`) before the server starts on the same bot.

The files the server runs from (`/opt/atya/docker-compose.yml`, `Caddyfile`,
`update.sh`) are kept in `deploy/server/`. `user-data.sh` embeds copies for a
fresh server; `enable-web.sh` installs them on the existing one.

## WhatsApp

WhatsApp runs next to Telegram once these are in SSM (then `deploy/deploy.sh`):

| Parameter | What |
| --- | --- |
| `WHATSAPP_TOKEN` | Permanent token (Meta app → WhatsApp → API Setup → Generate token) |
| `WHATSAPP_APP_SECRET` | App settings → Basic → App secret (checks webhook signatures) |
| `WHATSAPP_PHONE_NUMBER_ID` | API Setup → Phone number ID |
| `WHATSAPP_VERIFY_TOKEN` | Any random string; also pasted into the Meta webhook setup |
| `WHATSAPP_NUMBER` | The business number in digits, for wa.me links in invites |

Webhook in the Meta app (WhatsApp → Configuration): callback URL
`https://<WEB_DOMAIN>/whatsapp/webhook`, the verify token above, and subscribe
to the `messages` field. The web view must be on.

Users join with an invite (`/invite` gives a wa.me link) or connect WhatsApp to
an existing Telegram account with `/link`.

WhatsApp only allows free-form messages within 24 hours of the user's last
message. After that, reminders go out as the `atya_reminder` template (with
Done / snooze buttons), and anything else waits behind one `atya_brief` or
`atya_update` template ("tap Show"); it is sent as soon as the user taps or
writes. Templates need `WHATSAPP_WABA_ID` in SSM, a payment method on the
WhatsApp account, and Meta's approval:

```bash
deploy/run.sh "cd /opt/atya && docker compose exec -T app python -m atya.wa_templates create"
deploy/run.sh "cd /opt/atya && docker compose exec -T app python -m atya.wa_templates status"
```

## Trying other models (Amazon Bedrock)

`deploy/enable-bedrock.sh` (once) lets the server call Bedrock models. Then:

```bash
deploy/run.sh "cd /opt/atya && docker compose exec -T app python -m atya.eval --list"            # model IDs
deploy/run.sh "cd /opt/atya && docker compose exec -T app python -m atya.eval --ping ID1 ID2"     # quick check
deploy/run.sh "cd /opt/atya && docker compose exec -T app python -m atya.eval --fast FAST_ID --main MAIN_ID"
```

The last one replays your recent messages (read-only) and saves a report at
`/eval` on the web view (owner only). To switch models for real, set
`LLM_PROVIDER=bedrock`, `BEDROCK_FAST_MODEL` and `BEDROCK_MAIN_MODEL` in SSM and deploy.

## Voice notes

`deploy/enable-voice.sh` then `deploy/deploy.sh`. Voice notes on Telegram and
WhatsApp are transcribed with Amazon Transcribe (English (India) or Hindi,
detected automatically), then filed like typed messages; replies start with
"Heard: …". The audio is kept with the message (playable on the Messages page,
included in exports); the copy staged in S3 for transcription is deleted at once.

## Spending caps

The app holds a user's messages (kept, not processed; the user is told once a
day) when they pass a daily cap. Defaults, changed with SSM parameters then
`deploy/deploy.sh` (0 = no cap; the owner is exempt):

| Parameter | Default | Per |
| --- | --- | --- |
| `LIMIT_DAILY_MESSAGES` | 150 | user, day |
| `LIMIT_DAILY_VOICE_MINUTES` | 30 | user, day |
| `LIMIT_DAILY_PDF_PAGES` | 300 | user, day |
| `LIMIT_DAILY_TOKENS` | 1500000 | user, day |
| `LIMIT_DAILY_TOKENS_ALL` | 20000000 | all users, UTC day (the owner is told once if hit) |

`/usage` shows today's numbers. For a dollar alarm from AWS itself:
`deploy/budget-alert.sh <monthly USD> <email>`.

## Web research

`deploy/enable-research.sh` then `deploy/deploy.sh`. Messages like "research the
best physios in Noida" (or that the classifier reads as a request to look
something up on the web) are answered by Amazon Nova 2 Lite with web search,
with sources, and saved as a report (document, Kind: report). Capped per user
per day by `LIMIT_DAILY_RESEARCH` (default 10).

## Google Calendar (and contacts' birthdays)

Read-only. Users connect at `/connect` on the web view (or send `/connect`).
Events from yesterday to 30 days ahead are copied into Calendar records and
kept in sync every 15 minutes; birthdays in the next 30 days become reminder
tasks; the morning brief lists today's events. Tokens are encrypted with
`CONNECTIONS_KEY`.

Setup: create an OAuth client (Web application) in Google Cloud Console with
the redirect URI printed by `deploy/enable-google.sh`, enable the Google
Calendar API and People API, store the client id and secret in SSM, then run
`deploy/enable-google.sh` and `deploy/deploy.sh`. While the app is in Testing
mode, only the test users listed on the consent screen (up to 100) can connect.
