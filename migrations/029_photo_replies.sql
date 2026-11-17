-- Replies can carry a photo (sending a saved photo back when asked for it).
ALTER TABLE outbox ADD COLUMN attachment_id uuid REFERENCES attachments(id) ON DELETE SET NULL;
