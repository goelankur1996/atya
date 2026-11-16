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


def birthday_lines(tx: Tx, schema: Schema, tenant_id, today: dt.date) -> list[str]:
    """Today's birthdays, then the rest of the week's."""
    if "person" not in schema.types or "Birthday" not in schema.types["person"].fields:
        return []
    days = [today + dt.timedelta(days=i) for i in range(WEEK_DAYS + 1)]
    lines = []
    window = {d.strftime("%m-%d") for d in days}
    for p in tx.birthdays_on(tenant_id, days):
        for fname, what in (("Birthday", ""), ("Anniversary", " (anniversary)")):
            b = str(p["fields"].get(fname) or "")
            if len(b) < 5 or b[-5:] not in window:
                continue
            try:
                day = dt.date(today.year, int(b[-5:-3]), int(b[-2:]))
            except ValueError:
                continue
            if day < today:                               # the week runs into next year
                day = day.replace(year=today.year + 1)
            age = ""
            if len(b) == 10 and b[:4].isdigit():
                years = day.year - int(b[:4])
                age = f", turns {years}" if not what else f", {years} years"
            lines.append((day, f"- {p['title']}{what}{age} ({'today' if day == today else _day_word(day, today)})"))
    return [line for _, line in sorted(lines)]


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

    bdays = birthday_lines(tx, schema, tenant_id, today)
    if not (overdue or today_items or week):
        tail = f"{open_count} open task{'s' if open_count != 1 else ''}." if open_count else "No open tasks."
        cal = ("\n\nCalendar today\n" + "\n".join(calendar)) if calendar else ""
        bd = ("\n\nBirthdays\n" + "\n".join(bdays)) if bdays else ""
        return f"{head}{cal}{bd}\n\nNothing due today or this week. {tail}"

    parts = [head]
    if calendar:
        parts.append("Calendar today\n" + "\n".join(calendar))
    if bdays:
        parts.append("Birthdays\n" + "\n".join(bdays))
    for title, items in (("Overdue", overdue), ("Today", today_items), ("This week", week)):
        if items:
            extra = len(items) - MAX_PER_SECTION
            body = "\n".join(items[:MAX_PER_SECTION]) + (f"\n- and {extra} more" if extra > 0 else "")
            parts.append(f"{title}\n{body}")
    if waiting:
        parts.append(f"Waiting on others: {waiting} task{'s' if waiting != 1 else ''}.")
    return "\n\n".join(parts)


CALL_RECORDS_CHARS = 16000      # how much of their records a call starts with


def _short(value, n: int = 140) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def call_records(tx: Tx, schema: Schema, tenant_id, today: dt.date) -> str:
    """What a phone call starts out knowing, so common questions need no lookup: the calendar for the next
    week, open tasks (dated ones first), birthdays and anniversaries for a month, people and projects.
    Fetched when the call starts; reading only."""
    parts: list[str] = []
    if "event" in schema.types:
        days = []
        for i in range(WEEK_DAYS + 2):
            day = today + dt.timedelta(days=i)
            items = []
            for e in tx.events_on(tenant_id, day.isoformat()):
                start = str(e["fields"].get("Start", ""))
                when = "all day" if e["fields"].get("All Day") or len(start) <= 10 else _clock(start[11:16])
                where = f" at {e['fields']['Location']}" if e["fields"].get("Location") else ""
                items.append(f"  - {when}: {e['title']}{where}")
            if items:
                days.append(f"{day.strftime('%A %-d %B')}:\n" + "\n".join(items))
        if days:
            parts.append("Calendar, next 8 days:\n" + "\n".join(days))
    if "task" in schema.types:
        def task_line(r: dict) -> str:
            f = r["fields"]
            bits = [r["title"]]
            if f.get("Due Date"):
                try:
                    d = dt.date.fromisoformat(str(f["Due Date"])[:10])
                    bits.append(f"due {d.strftime('%a %-d %b')}" + (" (overdue)" if d < today else ""))
                except ValueError:
                    pass
            if f.get("Remind At"):
                bits.append(f"reminder {str(f['Remind At']).replace('T', ' ')}")
            if f.get("Notes"):
                bits.append(_short(f["Notes"]))
            return "- " + "; ".join(bits)
        until = (today + dt.timedelta(days=30)).isoformat()
        dated = tx.due_records(tenant_id, "task", until=until, closed=CLOSED_STATUSES, limit=80)
        seen = {r["id"] for r in dated}
        undated = [r for r in tx.list_records(tenant_id, "task", limit=200)
                   if r["id"] not in seen and r["fields"].get("Status") not in CLOSED_STATUSES][:60]
        if dated:
            parts.append("Open tasks with a date (to 30 days ahead):\n" + "\n".join(map(task_line, dated)))
        if undated:
            parts.append("Other open tasks (most recently touched first):\n" + "\n".join(map(task_line, undated)))
    if "person" in schema.types:
        days = [today + dt.timedelta(days=i) for i in range(31)]
        window = {d.strftime("%m-%d") for d in days}
        dates = []
        for p in tx.birthdays_on(tenant_id, days):
            for fname in ("Birthday", "Anniversary"):
                b = str(p["fields"].get(fname) or "")
                if len(b) >= 5 and b[-5:] in window:
                    dates.append(f"- {p['title']}: {fname.lower()} {b}")
        if dates:
            parts.append("Birthdays and anniversaries, next 30 days:\n" + "\n".join(dates))
        people = []
        for p in tx.list_records(tenant_id, "person", limit=80):
            f = p["fields"]
            about = [f"{k.lower()} {_short(f[k], 60)}" for k in ("Relationship", "Role", "Company", "Context")
                     if f.get(k)]
            people.append(f"- {p['title']}" + (f" ({'; '.join(about)})" if about else ""))
        if people:
            parts.append("People (most recently touched first):\n" + "\n".join(people))
    if "project" in schema.types:
        projects = [f"- {p['title']}" for p in tx.list_records(tenant_id, "project", limit=30)
                    if p["fields"].get("Status") in (None, "", "active")]
        if projects:
            parts.append("Active projects:\n" + "\n".join(projects))
    text = "\n\n".join(parts)
    if len(text) > CALL_RECORDS_CHARS:
        text = text[:CALL_RECORDS_CHARS].rsplit("\n", 1)[0] + "\n(more not shown: ask ATYA)"
    return text


def set_morning_brief(store: Store, tenant: dict, when: str, now: dt.datetime | None = None) -> str:
    """The daily brief's time, or 'off'. The one function behind /brief and the call's tool."""
    tid = tenant["id"]
    if when.strip().lower() in ("off", "stop", "no", "none"):
        with store.tx(tid) as tx:
            tx.set_brief(tid, None)
        return "Morning brief turned off."
    at = parse_time(when)
    if at is None:
        return "That time wasn't clear. Try a time like 9am, 8:30 or 19:00."
    local = local_now(tenant.get("timezone") or "UTC", now)
    skip = local.date() if local.time() >= at else None     # already past it today: start tomorrow
    with store.tx(tid) as tx:
        tx.set_brief(tid, at, skip_until=skip)
    return (f"Done. Every day at {fmt_time(at)} ({tenant.get('timezone') or 'UTC'}) I'll send what's overdue, due "
            f"today and due this week. First one {'tomorrow' if skip else 'today'}.")


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
