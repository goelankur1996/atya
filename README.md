# ATYA

Chat-first personal memory: capture anything through Telegram, have it sorted
into typed records, and ask questions answered from your own data.

## Status

- `assistant_bot/` — v1 (Telegram → Notion). Still the live system; untouched.
- `atya/` — v2, being built milestone by milestone (see the v2 spec).
  - **Milestone 1 (done):** Postgres tables, storage layer, schema file.
  - Next: receiver / processor / sender on Postgres.

## Layout (v2)

| Path | What |
| --- | --- |
| `migrations/001_init.sql` | Tables: tenants, inbox, records, record_links, outbox, audit_log, attachments, login tokens, sessions. Guards: inbox raw text is immutable, audit log is append-only. |
| `schema/default.yaml` | Record types, fields, vocabularies, routing and hard rules. Converted from the Notion System Schema page. |
| `atya/schema.py` | Loads and checks the schema file; validates every write. |
| `atya/store.py` | All database access. Every change writes its audit row in the same transaction. |
| `atya/migrate.py` | Applies pending migrations. |
| `tests/` | Schema tests (no database needed) and storage tests (need Postgres + pgvector). |

## Running

```bash
pip install -r requirements-dev.txt

# Postgres 16 with pgvector, e.g.:
docker run -d --name atya-db -e POSTGRES_PASSWORD=dev -p 5432:5432 pgvector/pgvector:pg16

DATABASE_URL=postgresql://postgres:dev@localhost:5432/atya python -m atya.migrate
TEST_DATABASE_URL=postgresql://postgres:dev@localhost:5432/atya_test pytest -q
```

The test suite drops and recreates the `TEST_DATABASE_URL` database, so never
point it at real data.
