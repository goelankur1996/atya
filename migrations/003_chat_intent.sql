-- Third message kind: chit-chat (greetings, thanks). Replied to, nothing saved.
ALTER TABLE inbox DROP CONSTRAINT inbox_intent_check;
ALTER TABLE inbox ADD CONSTRAINT inbox_intent_check CHECK (intent IN ('log', 'query', 'chat'));
