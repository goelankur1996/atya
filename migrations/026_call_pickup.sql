-- When the user picked up (Plivo fetched the answer URL), so the opening can start a set time after it.
ALTER TABLE calls ADD COLUMN picked_up_at timestamptz;
