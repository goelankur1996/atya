-- Morning brief: a daily message listing overdue / today / this-week tasks.
-- Off unless the user turns it on with /brief <time> (their own timezone).
ALTER TABLE tenants ADD COLUMN brief_time time;          -- local time of day; NULL = off
ALTER TABLE tenants ADD COLUMN brief_last_date date;     -- local date of the last brief sent
