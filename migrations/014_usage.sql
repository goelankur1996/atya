-- Daily usage per user, for spending caps (days are in the user's timezone).
CREATE TABLE usage_daily (
    tenant_id      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    day            date NOT NULL,
    messages       integer NOT NULL DEFAULT 0,
    voice_seconds  integer NOT NULL DEFAULT 0,
    pdf_pages      integer NOT NULL DEFAULT 0,
    tokens_in      bigint  NOT NULL DEFAULT 0,
    tokens_out     bigint  NOT NULL DEFAULT 0,
    held           integer NOT NULL DEFAULT 0,      -- messages not processed because of a limit
    told_at        timestamptz,                     -- when the user was last told about a limit
    PRIMARY KEY (tenant_id, day)
);
ALTER TABLE usage_daily ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON usage_daily USING (atya_visible(tenant_id)) WITH CHECK (atya_visible(tenant_id));
GRANT SELECT, INSERT, UPDATE, DELETE ON usage_daily TO atya_app;
