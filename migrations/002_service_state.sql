-- Small key/value store for service state (e.g. the Telegram polling offset),
-- so the app needs no local volume.
CREATE TABLE service_state (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
