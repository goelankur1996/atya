-- Web research on request: a new message intent, and a daily count for the cap.
ALTER TABLE inbox DROP CONSTRAINT inbox_intent_check;
ALTER TABLE inbox ADD CONSTRAINT inbox_intent_check
    CHECK (intent IN ('log', 'query', 'chat', 'command', 'research'));
ALTER TABLE usage_daily ADD COLUMN research integer NOT NULL DEFAULT 0;
