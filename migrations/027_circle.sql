-- Circle: people who let each other ask ATYA to call them (e.g. check in on a parent's exercise),
-- with a short report back to whoever asked. Each direction is its own link, agreed to by the
-- person who gets called.
ALTER TABLE tenants ADD COLUMN call_language text NOT NULL DEFAULT 'english'
    CHECK (call_language IN ('english', 'hinglish', 'hindi'));

CREATE TABLE circle_invites (
    code        text PRIMARY KEY,
    from_tenant uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name        text NOT NULL,                      -- what the inviter calls them ("Dad")
    language    text CHECK (language IN ('english', 'hinglish', 'hindi')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_by     uuid REFERENCES tenants(id) ON DELETE SET NULL,
    used_at     timestamptz
);

CREATE TABLE circle_links (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    caller      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,   -- may ask ATYA to call...
    callee      uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,   -- ...this person (who agreed)
    name        text NOT NULL,                      -- what the caller calls the callee
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'active', 'ended')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    accepted_at timestamptz,
    ended_at    timestamptz,
    CHECK (caller <> callee)
);
CREATE UNIQUE INDEX circle_links_one_live ON circle_links (caller, callee) WHERE status <> 'ended';

-- A link is visible to both people in it; changes happen in system scope (they span two accounts).
ALTER TABLE circle_invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE circle_links ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON circle_invites USING (atya_visible(from_tenant)) WITH CHECK (atya_visible(NULL));
CREATE POLICY own_rows ON circle_links USING (atya_visible(caller) OR atya_visible(callee))
    WITH CHECK (atya_visible(NULL));
GRANT SELECT, INSERT, UPDATE, DELETE ON circle_invites, circle_links TO atya_app;

-- Check-in calls: placed for someone in the callee's circle; the report goes back to them.
ALTER TABLE calls DROP CONSTRAINT calls_kind_check;
ALTER TABLE calls ADD CONSTRAINT calls_kind_check CHECK (kind IN ('morning', 'reminder', 'now', 'setup', 'checkin'));
ALTER TABLE calls
    ADD COLUMN requested_by  uuid REFERENCES tenants(id) ON DELETE CASCADE,
    ADD COLUMN checkin_id    uuid,                  -- the checkin record (in the requester's account)
    ADD COLUMN transcript_id uuid;                  -- the call's transcript (inbox, callee's account)
CREATE UNIQUE INDEX calls_one_checkin_a_day ON calls (checkin_id, day) WHERE kind = 'checkin';
