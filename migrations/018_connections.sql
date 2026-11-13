-- Connected accounts (Google: calendar and contacts' birthdays, read-only).
-- Tokens are encrypted by the app (CONNECTIONS_KEY) before they are stored.
CREATE TABLE connections (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider        text NOT NULL CHECK (provider IN ('google')),
    account         text,                      -- the account's email, for display
    scopes          text NOT NULL,
    refresh_token   bytea NOT NULL,            -- encrypted
    access_token    bytea,                     -- encrypted
    expires_at      timestamptz,
    status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'error')),
    error           text,
    last_sync_at    timestamptz,
    sync_state      jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, provider)
);
ALTER TABLE connections ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON connections USING (atya_visible(tenant_id)) WITH CHECK (atya_visible(tenant_id));
GRANT SELECT, INSERT, UPDATE, DELETE ON connections TO atya_app;

-- Synced data enters through the inbox like everything else: one row per sync that changed something.
ALTER TABLE inbox DROP CONSTRAINT inbox_source_check;
ALTER TABLE inbox ADD CONSTRAINT inbox_source_check
    CHECK (source IN ('telegram', 'slack', 'email', 'whatsapp', 'web', 'manual', 'voice', 'screenshot',
                      'granola', 'notion_doc', 'notion_import', 'google'));
