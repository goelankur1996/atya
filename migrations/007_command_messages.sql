-- Slash commands (/help, /web, /export...) are now kept in the inbox too, with
-- the bot's replies in the outbox, so the full conversation can be reviewed.
ALTER TABLE inbox DROP CONSTRAINT inbox_intent_check;
ALTER TABLE inbox ADD CONSTRAINT inbox_intent_check CHECK (intent IN ('log', 'query', 'chat', 'command'));
CREATE INDEX inbox_by_time ON inbox (tenant_id, captured_at DESC);
CREATE INDEX outbox_by_inbox ON outbox (inbox_id);
