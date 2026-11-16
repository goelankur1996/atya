-- Every phone call ATYA places goes through one queue: the morning call, reminder
-- calls, "call me now", and the setup interview. Each row is one call with its own
-- retries; declining one call never affects another.
CREATE TABLE calls (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind         text NOT NULL CHECK (kind IN ('morning', 'reminder', 'now', 'setup')),
    about        text,                                   -- what a reminder call is for
    record_id    uuid REFERENCES records(id) ON DELETE SET NULL,
    day          date,                                   -- morning calls: the user's local day
    due_at       timestamptz NOT NULL,
    next_at      timestamptz,                            -- when to retry, if it wasn't answered
    attempts     integer NOT NULL DEFAULT 0,
    status       text NOT NULL DEFAULT 'pending' CHECK (status IN (
                     'pending', 'calling', 'answered', 'declined', 'missed', 'failed', 'unreachable',
                     'no-minutes', 'cancelled')),
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);
CREATE UNIQUE INDEX calls_one_morning ON calls (tenant_id, day) WHERE kind = 'morning';
CREATE INDEX calls_pending ON calls (coalesce(next_at, due_at)) WHERE status = 'pending';
ALTER TABLE calls ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON calls USING (atya_visible(tenant_id)) WITH CHECK (atya_visible(tenant_id));
GRANT SELECT, INSERT, UPDATE, DELETE ON calls TO atya_app;
