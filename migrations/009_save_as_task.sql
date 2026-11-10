-- "Save as task": a question that found nothing gets a button under the reply;
-- tapping it re-files the original message as a capture.
ALTER TABLE outbox ADD COLUMN buttons jsonb;          -- [[["label", "callback data"], ...], ...]
ALTER TABLE inbox ADD COLUMN forced_intent text CHECK (forced_intent IN ('log'));
