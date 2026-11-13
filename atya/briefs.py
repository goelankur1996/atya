"""Worker 5: the morning brief.

Once a minute, finds users whose brief time has come in their own timezone
and who haven't had today's brief, and queues one message each in the outbox
(the sender delivers it). No AI call: the brief is built straight from the
user's tasks, so it costs nothing and can't invent anything.

Exactly once per local day: the "sent" mark and the outbox row are written in
one transaction, and the mark is claimed with a conditional UPDATE. If the
server was down at brief time, the brief goes out when it's back, the same day.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
import zoneinfo

from .config import Settings
from .schema import CLOSED_STATUSES, Schema, load_schema
from .store import Store, Tx

log = logging.getLogger("briefs")

CHECK_SECONDS = 60
WEEK_DAYS = 6          # "this week" = the next six days after today
MAX_PER_SECTION = 10


def parse_time(text: str) -> dt.time | None:
    """'9', '9am', '8:30', '08:30', '7pm', '19:00', '7.30 pm' -> time; None if unclear."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*", text.lower())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").replace(".", "")
    if suffix:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if suffix == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return dt.time(hour, minute)


def fmt_time(t: dt.time) -> str:
    """09:00 -> '9:00 am'."""
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d} {'am' if t.hour < 12 else 'pm'}"


def local_now(timezone: str, now: dt.datetime | None = None) -> dt.datetime:
    try:
        tz = zoneinfo.ZoneInfo(timezone)
    except Exception:
        tz = dt.timezone.utc
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)


def _day_word(day: dt.date, today: dt.date) -> str:
    delta = (day - today).days
    if delta == 1:
        return "tomorrow"
    if 1 < delta <= WEEK_DAYS:
        return day.strftime("%a")
    return day.strftime("%-d %b")


def _since(day: dt.date, today: dt.date) -> str:
    delta = (today - day).days
    if delta == 1:
        return "since yesterday"
    if delta < 7:
        return f"since {day.strftime('%a')}"
    return f"since {day.strftime('%-d %b')}"


def _clock(hhmm: str) -> str:
    """'14:30' -> '2:30 pm'."""
    try:
        h, m = int(hhmm[:2]), int(hhmm[3:5])
    except ValueError:
        return hhmm
    return f"{h % 12 or 12}:{m:02d} {'am' if h < 12 else 'pm'}"


def build_brief(tx: Tx, schema: Schema, tenant_id, today: dt.date) -> str:
    """The brief's text for one user on one (local) day."""
    head = f"Good morning. {today.strftime('%A %-d %b')}"
    if "task" not in schema.types:
        return head
    prefix = schema.types["task"].prefix
    until = (today + dt.timedelta(days=WEEK_DAYS)).isoformat()
    due = tx.due_records(tenant_id, "task", until=until, closed=CLOSED_STATUSES, limit=50)

    overdue, today_items, week = [], [], []
    for r in due:
        try:
            day = dt.date.fromisoformat(str(r["fields"].get("Due Date", ""))[:10])
        except ValueError:
            continue
        line = f"- {prefix}-{r['seq']} {r['title']}"
        if day < today:
            overdue.append(f"{line} ({_since(day, today)})")
        elif day == today:
            today_items.append(line)
        else:
            week.append(f"{line} ({_day_word(day, today)})")

    open_count = tx.count_open(tenant_id, "task", CLOSED_STATUSES)
    waiting = tx.count_open(tenant_id, "task", CLOSED_STATUSES, status="waiting") \
        if "waiting" in getattr(schema.types["task"].fields.get("Status"), "options", ()) else 0

    calendar = []
    if "event" in schema.types:
        for e in tx.events_on(tenant_id, today.isoformat())[:MAX_PER_SECTION]:
            start = str(e["fields"].get("Start", ""))
            when = "all day" if e["fields"].get("All Day") or len(start) <= 10 else _clock(start[11:16])
            where = f" ({e['fields']['Location']})" if e["fields"].get("Location") else ""
            calendar.append(f"- {when}: {e['title']}{where}")

    if not (overdue or today_items or week):
        tail = f"{open_count} open task{'s' if open_count != 1 else ''}." if open_count else "No open tasks."
        cal = ("\n\nCalendar today\n" + "\n".join(calendar)) if calendar else ""
        return f"{head}{cal}\n\nNothing due today or this week. {tail}"

    parts = [head]
    if calendar:
        parts.append("Calendar today\n" + "\n".join(calendar))
    for title, items in (("Overdue", overdue), ("Today", today_items), ("This week", week)):
        if items:
            extra = len(items) - MAX_PER_SECTION
            body = "\n".join(items[:MAX_PER_SECTION]) + (f"\n- and {extra} more" if extra > 0 else "")
            parts.append(f"{title}\n{body}")
    if waiting:
        parts.append(f"Waiting on others: {waiting} task{'s' if waiting != 1 else ''}.")
    return "\n\n".join(parts)


def send_due_briefs(store: Store, now: dt.datetime | None = None) -> int:
    """Queue every brief that's due right now. Returns how many were queued."""
    with store.tx(system=True) as tx:
        candidates = tx.brief_candidates()
    sent = 0
    for c in candidates:
        local = local_now(c["timezone"], now)
        if local.time() < c["brief_time"]:
            continue
        if c["brief_last_date"] is not None and c["brief_last_date"] >= local.date():
            continue
        try:
            schema = load_schema(c["schema_name"])
            with store.tx(c["id"]) as tx:
                if not tx.claim_brief(c["id"], local.date()):
                    continue                     # another run got there first
                body = build_brief(tx, schema, c["id"], local.date())
                tx.add_outbox(c["id"], chat_id=c["telegram_chat_id"], body=body, actor="briefs", kind="brief")
            sent += 1
        except Exception:
            log.exception("brief for tenant %s failed; will retry next minute", c["id"])
    if sent:
        log.info("queued %d morning brief(s)", sent)
    return sent


def run(store: Store, settings: Settings) -> None:
    log.info("briefs started")
    while True:
        send_due_briefs(store)
        time.sleep(CHECK_SECONDS)
