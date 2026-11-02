-- Per-user isolation enforced by Postgres itself (row-level security).
--
-- The app connects as the database owner only to run migrations; for all
-- normal work every pooled connection does SET ROLE atya_app. That role is
-- subject to the policies below: it sees and writes only rows whose tenant
-- matches the transaction's atya.tenant_id setting. Queue workers that must
-- see every user's queue (claiming inbox/outbox rows, the embedder) open
-- transactions with atya.scope = 'system'. A transaction that sets neither
-- sees nothing at all.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'atya_app') THEN
        CREATE ROLE atya_app NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO atya_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO atya_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO atya_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO atya_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO atya_app;
-- The audit log stays append-only for the app (the trigger also blocks UPDATE).
REVOKE UPDATE ON audit_log FROM atya_app;

CREATE FUNCTION atya_visible(t uuid) RETURNS boolean LANGUAGE sql STABLE AS $$
    SELECT coalesce(current_setting('atya.scope', true), '') = 'system'
        OR t = nullif(current_setting('atya.tenant_id', true), '')::uuid
$$;

ALTER TABLE tenants      ENABLE ROW LEVEL SECURITY;
ALTER TABLE invites      ENABLE ROW LEVEL SECURITY;
ALTER TABLE seq_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE inbox        ENABLE ROW LEVEL SECURITY;
ALTER TABLE records      ENABLE ROW LEVEL SECURITY;
ALTER TABLE record_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE outbox       ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log    ENABLE ROW LEVEL SECURITY;
ALTER TABLE attachments  ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions     ENABLE ROW LEVEL SECURITY;

CREATE POLICY own_rows ON tenants      USING (atya_visible(id))         WITH CHECK (atya_visible(id));
CREATE POLICY own_rows ON invites      USING (atya_visible(created_by)) WITH CHECK (atya_visible(created_by));
CREATE POLICY own_rows ON seq_counters USING (atya_visible(tenant_id))  WITH CHECK (atya_visible(tenant_id));
CREATE POLICY own_rows ON inbox        USING (atya_visible(tenant_id))  WITH CHECK (atya_visible(tenant_id));
CREATE POLICY own_rows ON records      USING (atya_visible(tenant_id))  WITH CHECK (atya_visible(tenant_id));
CREATE POLICY own_rows ON record_links USING (atya_visible(tenant_id))  WITH CHECK (atya_visible(tenant_id));
CREATE POLICY own_rows ON outbox       USING (atya_visible(tenant_id))  WITH CHECK (atya_visible(tenant_id));
CREATE POLICY own_rows ON audit_log    USING (atya_visible(tenant_id))  WITH CHECK (atya_visible(tenant_id));
CREATE POLICY own_rows ON attachments  USING (atya_visible(tenant_id))  WITH CHECK (atya_visible(tenant_id));
CREATE POLICY own_rows ON login_tokens USING (atya_visible(tenant_id))  WITH CHECK (atya_visible(tenant_id));
CREATE POLICY own_rows ON sessions     USING (atya_visible(tenant_id))  WITH CHECK (atya_visible(tenant_id));

-- Onboarding: a tenant created from an invite stays 'invited' until they pick a timezone.
-- (tenants.status already allows 'invited'.)
