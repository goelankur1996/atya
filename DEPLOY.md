# Running the Personal Assistant Bot online

This is a single Python program (`assistant_bot/`) that runs three workers
(receiver / processor / sender) as threads in one process. They still coordinate
only through Notion — Inbox / Outbox dead-drops — so the architecture is
unchanged from the three-service version; this is just a smaller package.

## What runs

| Worker      | Job                                                              | State |
|-------------|------------------------------------------------------------------|-------|
| receiver    | Long-poll Telegram → write each message to Notion Inbox          | Polling offset on a volume |
| processor   | Inbox → classify with Claude → fan out to Notion / draft replies | none (rules read live from Notion schema page) |
| sender      | Outbox → deliver replies via Telegram                            | none |

A crash in one worker is contained and auto-restarts; the others keep running.

## Easiest deploy: Fly.io (one app, one machine, ~free)

1. Install the `fly` CLI and `fly auth login`.
2. From this directory:
   ```bash
   fly launch --no-deploy --copy-config        # uses the provided fly.toml
   fly volumes create receiver_state --size 1  # 1 GB; free tier
   fly secrets set \
       TELEGRAM_BOT_TOKEN=... \
       TELEGRAM_ALLOWED_USER_IDS=... \
       NOTION_TOKEN=... \
       INBOX_DATA_SOURCE_ID=... \
       OUTBOX_DATA_SOURCE_ID=... \
       SCHEMA_PAGE_ID=... \
       ANTHROPIC_API_KEY=...
   fly deploy
   fly logs                                    # watch the boot + first round-trip
   ```
3. **Stop the laptop copy first.** Telegram allows only one `getUpdates`
   consumer per bot; otherwise one side gets `409 Conflict`.

Why Fly: one container, persistent volume, auto-restart, per-second billing on
a `shared-cpu-1x/256MB` machine — cheapest always-on option that supports a
long-poll worker. Render's free tier sleeps background workers; Railway no
longer has a free background tier.

## Alternative: any VPS with Docker

```bash
docker build -t assistant-bot .
docker run -d --name assistant-bot --restart=unless-stopped \
    --env-file .env -v receiver-state:/app/state assistant-bot
docker logs -f assistant-bot
```

Or with compose:

```bash
docker compose up -d --build
docker compose logs -f
```

## Updating

```bash
git pull
fly deploy           # or: docker compose up -d --build
```

Schema/routing-rule edits do not need a redeploy — the processor re-reads the
Notion System Schema page every `SCHEMA_RELOAD_MINUTES` (default 15).

## Polling offset (optional cutover note)

The receiver stores the last Telegram update id at
`$STATE_DIR/telegram_polling.json` (default `/app/state/telegram_polling.json`)
so restarts neither miss nor duplicate messages. Notion-side dedup
(`Source Message ID`) prevents duplicates either way, so a fresh start is fine.

## 🔐 Security

Secrets live in `.env` (gitignored) for local Docker runs or `fly secrets` on
Fly. Keep `.env.example` blank-placeholdered. If the three pre-existing keys
(Telegram, Notion, Anthropic) were ever committed in plaintext, rotate them:

- Telegram: `/revoke` then `/token` in @BotFather
- Notion: regenerate at notion.so/profile/integrations
- Anthropic: roll the key at console.anthropic.com/settings/keys
