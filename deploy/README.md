# Deploying ATYA to AWS

One small server (EC2 t4g.small, arm64, us-east-1) runs Postgres + the app in
Docker. There is **no SSH and no open port**: the server is managed through
AWS Systems Manager with `deploy/run.sh`. Secrets live in SSM Parameter Store
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
| Change a secret | `aws ssm put-parameter --name /atya/<NAME> --value '...' --type SecureString --overwrite`, then `deploy/deploy.sh` |

`setup.sh` copies the bot token, owner ID, Anthropic and Voyage keys from your
local `.env`, and the Notion token from the v1 `.env`, into SSM.

Only one program may read a Telegram bot at a time. Stop the local copy
(`docker compose down`) before the server starts on the same bot.
