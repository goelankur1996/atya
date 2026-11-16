-- Circle by message: a question sent to someone on someone else's behalf, waiting for their reply.
CREATE TABLE circle_asks (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    requester    uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    callee       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name         text NOT NULL,                       -- what the requester calls them
    checkin_id   uuid,                                -- the checkin record (requester's account)
    day          date NOT NULL,
    question     text NOT NULL,                       -- what was asked (the requester's words)
    sent_text    text NOT NULL,                       -- the message the callee got
    sent_at      timestamptz NOT NULL DEFAULT now(),
    wait_until   timestamptz NOT NULL,
    status       text NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'answered', 'expired')),
    answer       text,
    answered_at  timestamptz
);
CREATE UNIQUE INDEX circle_asks_one_a_day ON circle_asks (checkin_id, day) WHERE checkin_id IS NOT NULL;
CREATE INDEX circle_asks_waiting ON circle_asks (callee) WHERE status = 'waiting';
ALTER TABLE circle_asks ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON circle_asks USING (atya_visible(requester) OR atya_visible(callee))
    WITH CHECK (atya_visible(NULL));
GRANT SELECT, INSERT, UPDATE, DELETE ON circle_asks TO atya_app;
