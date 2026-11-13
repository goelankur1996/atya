-- WhatsApp as a second channel next to Telegram.
--
-- A user can have Telegram, WhatsApp or both. Replies go back where the
-- message came from; messages nobody asked for (reminders, the morning brief)
-- go to the channel the user last wrote from (tenants.channel).
ALTER TABLE tenants
    ADD COLUMN whatsapp_phone        text UNIQUE,              -- WhatsApp id: the number in digits, e.g. 919711966131
    ADD COLUMN whatsapp_last_inbound timestamptz,              -- free-form replies only within 24 h of this
    ADD COLUMN channel               text NOT NULL DEFAULT 'telegram'
                                     CHECK (channel IN ('telegram', 'whatsapp'));

-- The WhatsApp message id of a sent reply, so a later delivery failure
-- (reported by webhook) can be recorded against it.
ALTER TABLE outbox ADD COLUMN channel_message_id text;
CREATE INDEX outbox_by_channel_message ON outbox (channel_message_id) WHERE channel_message_id IS NOT NULL;

-- Webhook calls from Meta, stored as received and processed by a worker, so
-- the webhook answers at once and nothing is lost if processing fails.
-- A row is deleted once processed; rows that keep failing stay for a look.
CREATE TABLE whatsapp_events (
    id           bigserial PRIMARY KEY,
    payload      jsonb NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    attempts     integer NOT NULL DEFAULT 0,
    error        text
);
ALTER TABLE whatsapp_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY system_only ON whatsapp_events
    USING (coalesce(current_setting('atya.scope', true), '') = 'system')
    WITH CHECK (coalesce(current_setting('atya.scope', true), '') = 'system');
GRANT SELECT, INSERT, UPDATE, DELETE ON whatsapp_events TO atya_app;
GRANT USAGE, SELECT ON SEQUENCE whatsapp_events_id_seq TO atya_app;
