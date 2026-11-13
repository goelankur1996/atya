-- WhatsApp outside the 24-hour window: reminders go out as an approved
-- template; anything else waits (status 'waiting') behind a short "tap Show"
-- template and is sent as soon as the user writes or taps.
ALTER TABLE outbox DROP CONSTRAINT IF EXISTS outbox_status_check;
ALTER TABLE outbox ADD CONSTRAINT outbox_status_check
    CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled', 'waiting'));
ALTER TABLE outbox ADD COLUMN kind text;                  -- 'brief', 'reminder' or NULL (a reply)
ALTER TABLE tenants ADD COLUMN whatsapp_nudged_at timestamptz;
