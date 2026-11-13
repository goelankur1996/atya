-- People who asked to join: verified by messaging ATYA from their own account.
CREATE TABLE waitlist (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    channel           text NOT NULL CHECK (channel IN ('whatsapp', 'telegram')),
    whatsapp_phone    text UNIQUE,
    telegram_user_id  bigint UNIQUE,
    telegram_chat_id  bigint,
    display_name      text,
    source            text NOT NULL DEFAULT 'chat',         -- 'web' (came from the waitlist page) or 'chat'
    want              text,                                 -- their answer to "what would you want help with?"
    last_inbound_at   timestamptz,                          -- WhatsApp: opens the 24-hour window
    created_at        timestamptz NOT NULL DEFAULT now(),
    CHECK ((channel = 'whatsapp') = (whatsapp_phone IS NOT NULL)),
    CHECK ((channel = 'telegram') = (telegram_user_id IS NOT NULL))
);
CREATE INDEX waitlist_order ON waitlist (created_at);
ALTER TABLE waitlist ENABLE ROW LEVEL SECURITY;
CREATE POLICY system_only ON waitlist
    USING (coalesce(current_setting('atya.scope', true), '') = 'system')
    WITH CHECK (coalesce(current_setting('atya.scope', true), '') = 'system');
GRANT SELECT, INSERT, UPDATE, DELETE ON waitlist TO atya_app;
