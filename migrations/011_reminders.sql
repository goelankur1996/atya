-- Timed reminders: a task's "Remind At" field (local time) triggers one
-- Telegram message. This table records which (task, time) pairs were sent,
-- so each reminder goes out exactly once and a changed time is a new reminder.
CREATE TABLE reminders_sent (
    tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    record_id  uuid NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    remind_at  text NOT NULL,                  -- the field value as it was when sent
    sent_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id, remind_at)
);
CREATE INDEX reminders_sent_by_tenant ON reminders_sent (tenant_id);
CREATE INDEX records_remind_at ON records ((fields->>'Remind At')) WHERE type = 'task' AND fields ? 'Remind At';

ALTER TABLE reminders_sent ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON reminders_sent USING (atya_visible(tenant_id)) WITH CHECK (atya_visible(tenant_id));
GRANT SELECT, INSERT, DELETE ON reminders_sent TO atya_app;
