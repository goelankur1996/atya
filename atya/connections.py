"""Keeping connected Google accounts in sync (read-only).

Every SYNC_EVERY minutes, for each connected account:
- Calendar: events from yesterday to WINDOW_DAYS ahead, on the calendars the
  user shows in Google Calendar, are copied into event records (one per
  event, matched by Google's id; changes and cancellations are applied).
- Contacts: a birthday in the next BIRTHDAY_DAYS becomes a "Wish X a happy
  birthday" task with a 9 am reminder; the person's Birthday field is filled
  (for people already in the records, whenever it's known).

Changes enter through one inbox row per sync (source 'google'), like any
other capture. Automation never overwrites a field the user edited.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .google import GoogleAPI, GoogleError, GoogleOAuth, Vault
from .schema import load_schema
from .store import Store, Tx

log = logging.getLogger("connections")

SYNC_EVERY = 15             # minutes
WINDOW_DAYS = 30
BIRTHDAY_DAYS = 30
MAX_ATTENDEES = 10


def local_value(value: dict, tz: ZoneInfo) -> tuple[str, bool]:
    """A Google start/end ({"dateTime"} or {"date"}) -> ("YYYY-MM-DDTHH:MM" local, False) or ("YYYY-MM-DD", True)."""
    if value.get("date"):
        return value["date"], True
    t = dt.datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")).astimezone(tz)
    return t.strftime("%Y-%m-%dT%H:%M:00"), False      # the stored form of date-time fields


def event_fields(ev: dict, calendar_name: str, tz: ZoneInfo) -> dict:
    start, all_day = local_value(ev.get("start") or {}, tz)
    end, _ = local_value(ev.get("end") or {}, tz) if ev.get("end") else ("", all_day)
    people = [a.get("displayName") or a.get("email", "") for a in ev.get("attendees") or []
              if not a.get("self") and not a.get("resource")]
    fields = {"Start": start, "End": end, "All Day": all_day, "Calendar": calendar_name,
              "Status": ev.get("status") if ev.get("status") in ("confirmed", "tentative", "cancelled") else "confirmed",
              "Location": (ev.get("location") or "")[:300], "Link": ev.get("htmlLink") or "",
              "Attendees": ", ".join(p for p in people[:MAX_ATTENDEES] if p)
                           + (f" and {len(people) - MAX_ATTENDEES} more" if len(people) > MAX_ATTENDEES else "")}
    return {k: v for k, v in fields.items() if v not in ("", None)}


def next_birthday(month: int, day: int, today: dt.date) -> dt.date | None:
    for year in (today.year, today.year + 1):
        try:
            d = dt.date(year, month, day)
        except ValueError:            # 29 Feb in a non-leap year: the 28th
            d = dt.date(year, 2, 28) if (month, day) == (2, 29) else None
        if d and d >= today:
            return d
    return None


class Sync:
    """One sync of one account. Creates the sync's inbox row only if something changes."""

    def __init__(self, tx: Tx, tenant: dict, now: dt.datetime) -> None:
        self.tx, self.tenant, self.now = tx, tenant, now
        self.tid = tenant["tenant_id"]
        self.schema = load_schema(tenant["schema_name"])
        self.tz = ZoneInfo(tenant.get("timezone") or "UTC")
        self.inbox_id = None
        self.counts = {"new events": 0, "changed events": 0, "cancelled events": 0, "birthday reminders": 0,
                       "birthdays filled": 0}

    def _inbox(self):
        if self.inbox_id is None:
            self.inbox_id = self.tx.add_inbox(self.tid, source="google", status="processing", actor="google",
                                              raw_content="Google Calendar and Contacts sync",
                                              captured_at=self.now)
        return self.inbox_id

    def event(self, ev: dict, calendar_id: str, calendar_name: str) -> None:
        ext = f"gcal:{calendar_id}:{ev['id']}"
        existing = self.tx.record_by_external(self.tid, ext)
        if ev.get("status") == "cancelled":
            if existing and existing["fields"].get("Status") != "cancelled":
                self.tx.update_record(self.tid, self.schema, existing["id"], {"Status": "cancelled"}, actor="google")
                self.counts["cancelled events"] += 1
            return
        if not (ev.get("start") or {}).get("date") and not (ev.get("start") or {}).get("dateTime"):
            return
        fields = event_fields(ev, calendar_name, self.tz)
        title = (ev.get("summary") or "(no title)").strip()
        if existing is None:
            self.tx.create_record(self.tid, self.schema, "event", title=title, fields=fields,
                                  source_inbox_id=self._inbox(), actor="google", external_id=ext)
            self.counts["new events"] += 1
            return
        changed = {k: v for k, v in fields.items() if existing["fields"].get(k) != v}
        if changed or existing["title"] != title:
            self._inbox()
            if changed:
                self.tx.update_record(self.tid, self.schema, existing["id"], changed, actor="google")
            if existing["title"] != title:
                self.tx.set_title(self.tid, existing["id"], title)
            self.counts["changed events"] += 1

    def birthday(self, b: dict, today: dt.date) -> None:
        when = next_birthday(b["month"], b["day"], today)
        md = f"{b['year']:04d}-{b['month']:02d}-{b['day']:02d}" if b.get("year") else f"{b['month']:02d}-{b['day']:02d}"
        person = self.tx._one("SELECT * FROM records WHERE tenant_id = %s AND type = 'person' "
                              "AND lower(title) = lower(%s) LIMIT 1", (self.tid, b["name"]))
        soon = when is not None and (when - today).days <= BIRTHDAY_DAYS
        if person is None and not soon:
            return
        if person is None:
            person, _ = self.tx.create_record(self.tid, self.schema, "person", title=b["name"],
                                              fields={"Birthday": md}, source_inbox_id=self._inbox(),
                                              actor="google", external_id=f"gperson:{b['id']}")
            self.counts["birthdays filled"] += 1
        elif not person["fields"].get("Birthday") and "Birthday" in self.schema.types["person"].fields:
            self._inbox()
            self.tx.update_record(self.tid, self.schema, person["id"], {"Birthday": md}, actor="google")
            self.counts["birthdays filled"] += 1
        if soon and "task" in self.schema.types:
            ext = f"gbday:{b['id']}:{when.year}"
            if self.tx.record_by_external(self.tid, ext) is None:
                self.tx.create_record(self.tid, self.schema, "task", title=f"Wish {b['name']} a happy birthday",
                                      fields={"Status": "next", "Due Date": when.isoformat(),
                                              "Remind At": f"{when.isoformat()}T09:00"},
                                      source_inbox_id=self._inbox(), actor="google", external_id=ext)
                self.counts["birthday reminders"] += 1

    def finish(self) -> str:
        summary = ", ".join(f"{n} {k}" for k, n in self.counts.items() if n) or "no changes"
        if self.inbox_id is not None:
            self.tx.finish_inbox(self.inbox_id, status="processed", intent="log", notes=f"Google sync: {summary}")
        return summary


def sync_one(store: Store, oauth: GoogleOAuth, vault: Vault, conn: dict, *, http: Any = None,
             now: dt.datetime | None = None) -> str:
    """Sync one connection. Returns a short summary; raises GoogleError on failure."""
    now = now or dt.datetime.now(dt.timezone.utc)
    access = vault.open(conn["access_token"])
    if not access or not conn["expires_at"] or conn["expires_at"] < now + dt.timedelta(minutes=2):
        tok = oauth.refresh(vault.open(conn["refresh_token"]))
        access = tok["access_token"]
        with store.tx(conn["tenant_id"]) as tx:
            tx.update_connection(conn["id"], access_token=vault.seal(access),
                                 expires_at=now + dt.timedelta(seconds=int(tok.get("expires_in", 3600))))
    api = GoogleAPI(access, http=http)
    tz = ZoneInfo(conn.get("timezone") or "UTC")
    today = now.astimezone(tz).date()
    calendars = api.calendars()
    events = [(c, ev) for c in calendars
              for ev in api.events(c["id"], now - dt.timedelta(days=1), now + dt.timedelta(days=WINDOW_DAYS))]
    try:
        birthdays = api.birthdays()
    except GoogleError as e:              # contacts access may be refused while calendar works
        log.warning("birthdays skipped for %s: %s", conn["tenant_id"], e)
        birthdays = []
    with store.tx(conn["tenant_id"]) as tx:
        sync = Sync(tx, conn, now)
        for c, ev in events:
            sync.event(ev, c["id"], c.get("summaryOverride") or c.get("summary") or "Calendar")
        for b in birthdays:
            sync.birthday(b, today)
        summary = sync.finish()
        tx.update_connection(conn["id"], last_sync_at=now, status="active", error=None)
    return summary


def tell_reconnect(store: Store, conn: dict, why: str) -> None:
    with store.tx(conn["tenant_id"]) as tx:
        tx.update_connection(conn["id"], status="error", error=why[:500])
        tx.add_outbox(conn["tenant_id"], chat_id=conn.get("telegram_chat_id"), actor="google",
                      body="Your Google Calendar connection stopped working (access was removed or expired), "
                           "so I've stopped syncing it. To reconnect, send /connect.")


def sync_due(store: Store, oauth: GoogleOAuth, vault: Vault) -> int:
    with store.tx(system=True) as tx:
        due = tx.connections_due(SYNC_EVERY)
    for conn in due:
        try:
            summary = sync_one(store, oauth, vault, conn)
            log.info("google sync %s: %s", str(conn["tenant_id"])[:4], summary)
        except GoogleError as e:
            if e.revoked:
                tell_reconnect(store, conn, str(e))
            else:
                log.warning("google sync %s failed: %s", str(conn["tenant_id"])[:4], e)
                with store.tx(conn["tenant_id"]) as tx:
                    tx.update_connection(conn["id"], error=str(e)[:500], last_sync_at=dt.datetime.now(dt.timezone.utc))
        except Exception:
            log.exception("google sync %s crashed", str(conn["tenant_id"])[:4])
    return len(due)


def run(store: Store, settings: Settings) -> None:
    oauth = GoogleOAuth(settings.google_client_id, settings.google_client_secret)
    vault = Vault(settings.connections_key)
    log.info("google sync started (every %s min)", SYNC_EVERY)
    while True:
        try:
            sync_due(store, oauth, vault)
        except Exception:
            log.exception("google sync loop error")
        time.sleep(60)
