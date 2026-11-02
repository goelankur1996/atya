# ATYA

Chat-first personal memory: capture anything through Telegram, have it sorted
into typed records, and ask questions answered from your own data.

## Status

- `assistant_bot/` — v1 (Telegram → Notion). Still the live system; untouched.
- `atya/` — v2, being built milestone by milestone (see the v2 spec).
  - **Milestone 1 (done):** Postgres tables, storage layer, schema file.
  - **Milestone 2 (done, tested live):** receiver / processor / sender on Postgres.
  - **Milestone 3 (done, tested live):** hybrid search: full text + Voyage embeddings.
  - **Milestone 4 (done):** Notion history imported; running on AWS; real bot cut over.
  - **Milestone 5 (code done, deploy pending):** row-level security, invites and onboarding, /export, /delete.
  - Next: web view.

## Layout (v2)

| Path | What |
| --- | --- |
| `migrations/006_row_level_security.sql` | Postgres row-level security: the app runs as `atya_app` and every transaction is scoped to one user (`Store.tx(tenant_id)`) or explicitly to the system (`Store.tx(system=True)`); an unscoped transaction sees nothing. |
| `migrations/001_init.sql` | Tables: tenants, inbox, records, record_links, outbox, audit_log, attachments, login tokens, sessions. Guards: inbox raw text is immutable, audit log is append-only. |
| `schema/default.yaml` | Record types, fields, vocabularies, routing and hard rules. Converted from the Notion System Schema page. |
| `atya/schema.py` | Loads and checks the schema file; validates every write. |
| `atya/store.py` | All database access. Every change writes its audit row in the same transaction. |
| `atya/migrate.py` | Applies pending migrations. |
| `atya/receiver.py` | Telegram long-poll -> inbox (no AI). Polling offset kept in the database. |
| `atya/processor.py` | Claims inbox rows, classifies, then writes records (capture) or answers (question). All writes for one message commit together. |
| `atya/sender.py` | Outbox -> Telegram. |
| `atya/llm.py` | The four Claude calls (prompts ported from v1). |
| `atya/notion_import.py` | One-time, re-runnable import of the v1 Notion OS (inbox, 11 databases, relations, outbox, audit log). Keeps Notion IDs (TSK-41 stays TSK-41). `--dry-run` writes nothing. |
| `atya/commands.py` | `/start` (invite codes), `/invite` (owner), `/help`, `/export`, `/delete`; timezone onboarding; one polite reply a day to strangers. |
| `atya/export.py` | Builds a user's zip: raw inbox, CSV per type, Markdown per record, audit log, schema. |
| `atya/embeddings.py` | Voyage client + background worker that (re-)embeds records whose content changed. Optional: without `VOYAGE_API_KEY`, search is full text only. |
| `atya/__main__.py` | Runs migrations, then the three workers as supervised threads. |
| `tests/` | Schema tests (no database needed) and storage tests (need Postgres + pgvector). |

## Running the bot locally

```bash
cp .env.example .env      # test bot token, your Telegram user ID, Anthropic key
docker compose up -d --build
docker compose logs -f app
```

Only one copy of a bot may poll Telegram at a time, so use a separate test bot
while v1 is still running on the real one. `DEPLOY.md` describes v1 and is out
of date for v2.

## Tests

```bash
pip install -r requirements-dev.txt

# Postgres 16 with pgvector, e.g.:
docker run -d --name atya-db -e POSTGRES_PASSWORD=dev -p 5432:5432 pgvector/pgvector:pg16

DATABASE_URL=postgresql://postgres:dev@localhost:5432/atya python -m atya.migrate
TEST_DATABASE_URL=postgresql://postgres:dev@localhost:5432/atya_test pytest -q
```

The test suite drops and recreates the `TEST_DATABASE_URL` database, so never
point it at real data.
