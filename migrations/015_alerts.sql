-- Failure alerts to the owner: each failed message, failed send and stuck
-- WhatsApp event is reported once (alerted_at). Existing failures count as
-- already reported, so switching this on doesn't send a backlog.
ALTER TABLE inbox ADD COLUMN alerted_at timestamptz;
ALTER TABLE outbox ADD COLUMN alerted_at timestamptz;
ALTER TABLE whatsapp_events ADD COLUMN alerted_at timestamptz;
UPDATE inbox SET alerted_at = now() WHERE status = 'failed';
UPDATE outbox SET alerted_at = now() WHERE status = 'failed';
UPDATE whatsapp_events SET alerted_at = now() WHERE error IS NOT NULL;
CREATE INDEX inbox_unalerted_failures ON inbox (id) WHERE status = 'failed' AND alerted_at IS NULL;
CREATE INDEX outbox_unalerted_failures ON outbox (id) WHERE status = 'failed' AND alerted_at IS NULL;
