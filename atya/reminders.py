"""Worker 6: timed reminders for tasks ("remind me to call Jim at 2 pm").

A task's "Remind At" field holds a local date-time (no offset; the user's
timezone applies). Once a minute this worker finds reminders that are due and
queues one Telegram message each, with Done / In 1 hour / Tomorrow 9am buttons.

Exactly once: a (task, Remind At value) pair is claimed in reminders_sent in
the same transaction as the outbox row. Changing the time makes a new pair, so
it fires again; marking the task done stops it. A reminder missed while the
server was down goes out when it's back, marked with the time it was due.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import zoneinfo

from .config import Settings
from .schema import CLOSED_STATUSES, load_schema
from .store import Store

log = logging.getLogger("reminders")

CHECK_SECONDS = 30
LATE_AFTER = dt.timedelta(minutes=10)      # say "was due ..." when later than this
GIVE_UP_AFTER = dt.timedelta(days=7)        # don't fire reminders older than this
SNOOZE = dt.timedelta(hours=1)
TOMORROW_AT = dt.time(9, 0)


def tzinfo(name: str | None) -> dt.tzinfo:
    try:
        return zoneinfo.ZoneInfo(name or "UTC")
    except Exception:
        return dt.timezone.utc


def parse_at(value: str, tz: dt.tzinfo) -> dt.datetime | None:
    """Remind At (local, or with an offset) -> aware datetime; None if unusable."""
    try:
        if len(value.strip()) == 10:                      # a bare date: 9 am that day
            d = dt.date.fromisoformat(value.strip())
            return dt.datetime.combine(d, TOMORROW_AT, tzinfo=tz)
        at = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return at.replace(tzinfo=tz) if at.tzinfo is None else at


def local_value(at: dt.datetime, tz: dt.tzinfo) -> str:
    """Aware datetime -> the field's local form, YYYY-MM-DDTHH:MM:00."""
    return at.astimezone(tz).replace(tzinfo=None, second=0, microsecond=0).isoformat()


def human(at: dt.datetime, now: dt.datetime) -> str:
    """'2:00 pm today' / '9:00 am tomorrow' / '10:30 am Fri 14 Nov'."""
    local = at.astimezone(now.tzinfo)
    clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"
    delta = (local.date() - now.date()).days
    if delta == 0:
        return f"{clock} today"
    if delta == 1:
        return f"{clock} tomorrow"
    if delta == -1:
        return f"{clock} yesterday"
    return f"{clock} {local.strftime('%a %-d %b')}"


def buttons(record_id) -> list[list[tuple[str, str]]]:
    rid = str(record_id)
    return [[("Done", f"rem:done:{rid}"), ("In 1 hour", f"rem:1h:{rid}"), ("Tomorrow 9am", f"rem:tmrw:{rid}")]]


def send_due_reminders(store: Store, now: dt.datetime | None = None) -> int:
    now_utc = now or dt.datetime.now(dt.timezone.utc)
    with store.tx(system=True) as tx:
        candidates = tx.reminder_candidates(CLOSED_STATUSES)
    sent = 0
    for c in candidates:
        tz = tzinfo(c["timezone"])
        at = parse_at(c["remind_at"], tz)
        if at is None or at > now_utc or now_utc - at > GIVE_UP_AFTER:
            continue
        try:
            prefix = load_schema(c["schema_name"]).types["task"].prefix
            with store.tx(c["tenant_id"]) as tx:
                if not tx.claim_reminder(c["tenant_id"], c["id"], c["remind_at"]):
                    continue
                local_now = now_utc.astimezone(tz)
                late = f"\n(was due {human(at, local_now)})" if now_utc - at > LATE_AFTER else ""
                tx.add_outbox(c["tenant_id"], chat_id=c["telegram_chat_id"],
                              body=f"⏰ {c['title']} ({prefix}-{c['seq']}){late}",
                              buttons=buttons(c["id"]), priority="alert", actor="reminders", kind="reminder")
            sent += 1
        except Exception:
            log.exception("reminder for task %s failed; will retry", c["id"])
    if sent:
        log.info("queued %d reminder(s)", sent)
    return sent


def snooze_value(choice: str, tz: dt.tzinfo, now: dt.datetime | None = None) -> str:
    """New Remind At for a button: '1h' -> an hour from now, 'tmrw' -> 9 am tomorrow."""
    local_now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    if choice == "1h":
        at = local_now + SNOOZE
    else:
        at = dt.datetime.combine(local_now.date() + dt.timedelta(days=1), TOMORROW_AT, tzinfo=tz)
    return local_value(at, tz)


def run(store: Store, settings: Settings) -> None:
    log.info("reminders started")
    while True:
        send_due_reminders(store)
        time.sleep(CHECK_SECONDS)
