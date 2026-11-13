-- Signing in on the web with a code sent to WhatsApp or Telegram.
ALTER TABLE tenants ADD COLUMN telegram_username text;          -- lower case, no @; kept current by the receiver
CREATE INDEX tenants_by_telegram_username ON tenants (telegram_username) WHERE telegram_username IS NOT NULL;

CREATE TABLE signin_codes (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),     -- also the handle the browser holds
    tenant_id   uuid REFERENCES tenants(id) ON DELETE CASCADE,  -- NULL: nobody matched (same page either way)
    code_hash   text,
    channel     text,
    ip          text,
    attempts    integer NOT NULL DEFAULT 0,
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX signin_codes_recent ON signin_codes (created_at);
ALTER TABLE signin_codes ENABLE ROW LEVEL SECURITY;
CREATE POLICY system_only ON signin_codes
    USING (coalesce(current_setting('atya.scope', true), '') = 'system')
    WITH CHECK (coalesce(current_setting('atya.scope', true), '') = 'system');
GRANT SELECT, INSERT, UPDATE, DELETE ON signin_codes TO atya_app;
