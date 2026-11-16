-- Real phone calls (Plivo, from ATYA's Indian number) next to Telegram calls.
-- Each attempt gets a fresh secret: it is the only thing in the URLs Plivo calls back
-- (answer, hangup, audio stream), so it must not be guessable.
ALTER TABLE calls
    ADD COLUMN line          text CHECK (line IN ('phone', 'telegram')),   -- how the last attempt went out
    ADD COLUMN secret        text UNIQUE,
    ADD COLUMN provider_uuid text,                                         -- Plivo's call id
    ADD COLUMN answered_at   timestamptz,                                  -- the audio stream connected
    ADD COLUMN hangup_cause  text,                                         -- as Plivo reported it
    ADD COLUMN outcome       text;                                         -- set by the audio bridge when the talk ends
