-- ATYA v2 initial schema. Every row is scoped by tenant_id.
-- Applied by atya/migrate.py inside one transaction.

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector, for record embeddings

-- records.embedding is vector(1024): Voyage voyage-3.5 default output size.
-- Changing it later means a new column and a backfill (see EMBEDDING_DIM in atya/config.py).

-- ---------------------------------------------------------------------------
-- tenants: one row per user. Telegram is the account; email is for exports.
-- ---------------------------------------------------------------------------
CREATE TABLE tenants (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id  bigint UNIQUE,
    telegram_chat_id  bigint,
    display_name      text,
    email             text,
    timezone          text NOT NULL DEFAULT 'UTC',
    status            text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('invited', 'active', 'suspended')),
    schema_name       text NOT NULL DEFAULT 'default',
    schema_version    integer NOT NULL DEFAULT 1,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- One-time invite codes for /start <code>.
CREATE TABLE invites (
    code        text PRIMARY KEY,
    created_by  uuid REFERENCES tenants(id) ON DELETE CASCADE,
    used_by     uuid REFERENCES tenants(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    used_at     timestamptz
);

-- Per-tenant sequence numbers (inbox, and each record type: TSK-12).
CREATE TABLE seq_counters (
    tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    scope      text NOT NULL,
    value      integer NOT NULL,
    PRIMARY KEY (tenant_id, scope)
);

-- ---------------------------------------------------------------------------
-- inbox: the raw capture ledger. raw_content is immutable (trigger below).
-- ---------------------------------------------------------------------------
CREATE TABLE inbox (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    seq                integer NOT NULL,
    source             text NOT NULL
                       CHECK (source IN ('telegram', 'slack', 'email', 'whatsapp', 'web',
                                         'manual', 'voice', 'screenshot', 'granola',
                                         'notion_doc', 'notion_import')),
    source_message_id  text,
    sender             text,
    raw_content        text NOT NULL,
    captured_at        timestamptz NOT NULL,
    status             text NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'processing', 'processed',
                                         'failed', 'deferred', 'responded')),
    intent             text CHECK (intent IN ('log', 'query')),
    attempts           integer NOT NULL DEFAULT 0,
    claimed_at         timestamptz,
    processed_at       timestamptz,
    processor_notes    text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, seq)
);
-- Dedup: one row per channel message per tenant.
CREATE UNIQUE INDEX inbox_dedup ON inbox (tenant_id, source, source_message_id)
    WHERE source_message_id IS NOT NULL;
CREATE INDEX inbox_queue ON inbox (status, captured_at) WHERE status IN ('pending', 'processing');

-- ---------------------------------------------------------------------------
-- records: every typed record (task, person, journal, child trackers...).
-- ---------------------------------------------------------------------------
CREATE TABLE records (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    type                text NOT NULL,
    seq                 integer NOT NULL,          -- per tenant+type: TSK-12
    title               text NOT NULL,
    body                text,
    fields              jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_inbox_id     uuid REFERENCES inbox(id) ON DELETE SET NULL,
    user_edited_fields  text[] NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    search_text         tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(body, '')), 'B') ||
        setweight(jsonb_to_tsvector('english', fields, '["string"]'), 'C')
    ) STORED,
    embedding           vector(1024),
    embedded_at         timestamptz,
    UNIQUE (tenant_id, type, seq)
);
CREATE INDEX records_by_type ON records (tenant_id, type, updated_at DESC);
CREATE INDEX records_by_source ON records (tenant_id, source_inbox_id);
CREATE INDEX records_search ON records USING gin (search_text);
CREATE INDEX records_embedding ON records USING hnsw (embedding vector_cosine_ops);

-- Relations (task -> project, thought -> person ...). Named after the field.
CREATE TABLE record_links (
    tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    from_id    uuid NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    field      text NOT NULL,
    to_id      uuid NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (from_id, field, to_id)
);
CREATE INDEX record_links_to ON record_links (to_id);

-- ---------------------------------------------------------------------------
-- outbox: replies waiting to be delivered.
-- ---------------------------------------------------------------------------
CREATE TABLE outbox (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    inbox_id             uuid REFERENCES inbox(id) ON DELETE SET NULL,
    channel              text NOT NULL DEFAULT 'telegram',
    chat_id              bigint NOT NULL,
    reply_to_message_id  bigint,
    body                 text NOT NULL,
    priority             text NOT NULL DEFAULT 'routine' CHECK (priority IN ('routine', 'alert')),
    status               text NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    scheduled_for        timestamptz,
    sent_at              timestamptz,
    error                text,
    attempts             integer NOT NULL DEFAULT 0,
    created_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX outbox_queue ON outbox (status, scheduled_for) WHERE status IN ('pending', 'sending');

-- ---------------------------------------------------------------------------
-- audit_log: append-only (trigger below rejects UPDATE and DELETE).
-- tenant_id has no FK so tenant deletion is an explicit, separate step.
-- ---------------------------------------------------------------------------
CREATE TABLE audit_log (
    id            bigserial PRIMARY KEY,
    tenant_id     uuid,
    actor         text NOT NULL,
    action        text NOT NULL CHECK (action IN ('create', 'update', 'delete', 'send', 'reject')),
    target_table  text NOT NULL,
    target_id     uuid,
    summary       text NOT NULL,
    source_data   jsonb,
    at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_by_tenant ON audit_log (tenant_id, at DESC);

-- ---------------------------------------------------------------------------
-- attachments: files kept in S3; only metadata here.
-- ---------------------------------------------------------------------------
CREATE TABLE attachments (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    inbox_id    uuid REFERENCES inbox(id) ON DELETE CASCADE,
    s3_key      text NOT NULL,
    mime_type   text,
    bytes       bigint,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Web login: one-time links (sent by the bot) and sessions. Hashes only.
-- ---------------------------------------------------------------------------
CREATE TABLE login_tokens (
    token_hash  text PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz
);

CREATE TABLE sessions (
    session_hash  text PRIMARY KEY,
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL
);

-- ---------------------------------------------------------------------------
-- Guards
-- ---------------------------------------------------------------------------

-- Inbox raw text, origin and capture time can never change.
CREATE FUNCTION inbox_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.raw_content IS DISTINCT FROM OLD.raw_content
       OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.source_message_id IS DISTINCT FROM OLD.source_message_id
       OR NEW.captured_at IS DISTINCT FROM OLD.captured_at
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
        RAISE EXCEPTION 'inbox raw fields are immutable (inbox %)', OLD.id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER inbox_immutable BEFORE UPDATE ON inbox
    FOR EACH ROW EXECUTE FUNCTION inbox_guard();

-- Audit log is append-only. Tenant deletion is the single exception and must
-- set the session flag atya.allow_audit_purge = 'on' explicitly.
CREATE FUNCTION audit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('atya.allow_audit_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_log is append-only';
END $$;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_guard();

CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER records_touch BEFORE UPDATE ON records
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
