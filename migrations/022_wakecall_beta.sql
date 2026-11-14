-- Wake-up calls for other users (opt-in beta): when they agreed, and call minutes used per day.
ALTER TABLE tenants ADD COLUMN wakecall_consent_at timestamptz;
ALTER TABLE usage_daily ADD COLUMN call_seconds integer NOT NULL DEFAULT 0;
