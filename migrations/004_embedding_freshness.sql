-- updated_at should move only when content changes, not when an embedding is
-- stored; otherwise every embed would mark the record stale again.
-- A record needs (re-)embedding when embedded_at IS NULL OR embedded_at < updated_at.
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.title, NEW.body, NEW.fields, NEW.type) IS DISTINCT FROM
       (OLD.title, OLD.body, OLD.fields, OLD.type) THEN
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END $$;

CREATE INDEX records_needs_embedding ON records (updated_at)
    WHERE embedding IS NULL OR embedded_at < updated_at;
