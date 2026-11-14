-- Morning wake-up call (owner only for now): when to call, in the user's local time. NULL = off.
ALTER TABLE tenants ADD COLUMN wakecall_time time;

-- What was said on a call enters the inbox like any other capture.
ALTER TABLE inbox DROP CONSTRAINT inbox_source_check;
ALTER TABLE inbox ADD CONSTRAINT inbox_source_check
    CHECK (source IN ('telegram', 'slack', 'email', 'whatsapp', 'web', 'manual', 'voice', 'screenshot',
                      'granola', 'notion_doc', 'notion_import', 'google', 'call'));
