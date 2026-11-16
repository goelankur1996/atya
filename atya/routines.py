"""Routines: daily commitments ATYA checks in on (a challenge like 75 Hard, a habit).

A routine record has a Start Date (day 1), an optional length (Days), a daily
Checklist and a Check-in At time (default 21:00, local). Each day is logged as a
routine_day record (done / partial / missed), from:
- the check-in message's buttons (All done / Missed some),
- a message ("did everything for 75 hard today"), filed by the processor,
- a call: the call's prompt includes the routine and the log_routine tool records it.

When a day is logged as missed, ATYA asks whether to restart from day 1 or keep the
count (it doesn't decide). After the last day, the routine is marked done.

This worker runs once a minute: at each routine's check-in time it asks about today
(unless today is already logged), and asks the restart question after a miss.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

from . import briefs
from .config import Settings
from .schema import load_schema
from .store import Store, Tx

log = logging.getLogger("routines")

CHECK_SECONDS = 60
DEFAULT_CHECKIN = dt.time(21, 0)


def checkin_time(routine: dict) -> dt.time:
    return briefs.parse_time(str(routine["fields"].get("Check-in At") or "")) or DEFAULT_CHECKIN


def start_date(routine: dict) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(routine["fields"].get("Start Date", ""))[:10])
    except ValueError:
        return None


def length(routine: dict) -> int | None:
    try:
        n = int(float(routine["fields"].get("Days")))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def day_number(routine: dict, day: dt.date) -> int | None:
    start = start_date(routine)
    return None if start is None or day < start else (day - start).days + 1


def label(routine: dict, day: dt.date) -> str:
    """'75 Hard, day 3 of 75'."""
    n, total = day_number(routine, day), length(routine)
    if n is None:
        return routine["title"]
    return f"{routine['title']}, day {n}" + (f" of {total}" if total else "")


def checklist(routine: dict) -> list[str]:
    return [line.strip(" -•\t") for line in str(routine["fields"].get("Checklist") or "").splitlines()
            if line.strip(" -•\t")]


def active(tx: Tx, tenant_id) -> list[dict]:
    return tx._all("SELECT * FROM records WHERE tenant_id = %s AND type = 'routine' "
                   "AND coalesce(fields->>'Status', 'active') = 'active' ORDER BY seq", (tenant_id,))


def find(tx: Tx, tenant_id, name: str) -> dict | None:
    """An active routine by (part of) its name; the only one if there's just one."""
    routines = active(tx, tenant_id)
    name = (name or "").strip().lower()
    hits = [r for r in routines if name and (name in r["title"].lower() or r["title"].lower() in name)]
    if len(hits) == 1:
        return hits[0]
    return routines[0] if len(routines) == 1 and not hits else None


def log_for(tx: Tx, tenant_id, routine_id, day: dt.date) -> dict | None:
    return tx._one(
        "SELECT d.* FROM records d JOIN record_links l ON l.from_id = d.id AND l.field = 'Routine' "
        "WHERE d.tenant_id = %s AND d.type = 'routine_day' AND l.to_id = %s AND d.fields->>'Date' = %s "
        "ORDER BY d.created_at DESC LIMIT 1", (tenant_id, routine_id, day.isoformat()))


def log_day(tx: Tx, tenant: dict, routine: dict, day: dt.date, result: str, missed: str = "",
            *, actor: str = "routines") -> dict:
    """Record (or update) one day of a routine."""
    schema = load_schema(tenant["schema_name"])
    fields = {"Date": day.isoformat(), "Result": result}
    if missed:
        fields["Missed Items"] = missed
    existing = log_for(tx, tenant["id"], routine["id"], day)
    if existing:
        tx.update_record(tenant["id"], schema, existing["id"], fields, actor=actor)
        return tx.record(tenant["id"], existing["id"])
    rec, _ = tx.create_record(tenant["id"], schema, "routine_day", title=label(routine, day),
                              fields={**fields, "Routine": [str(routine["id"])]}, enforce_verbatim=False,
                              actor=actor)
    return rec


def restart(tx: Tx, tenant: dict, routine: dict, missed_day: dt.date) -> dt.date:
    """Day 1 becomes the day after the missed one."""
    schema = load_schema(tenant["schema_name"])
    new_start = missed_day + dt.timedelta(days=1)
    reached = (day_number(routine, missed_day) or 1) - 1
    notes = str(routine["fields"].get("Notes") or "")
    line = f"Restarted on {new_start.isoformat()} after a miss on day {reached + 1} ({missed_day.isoformat()})."
    tx.update_record(tenant["id"], schema, routine["id"],
                     {"Start Date": new_start.isoformat(), "Notes": (notes + "\n" + line).strip()},
                     actor="routines", by_user=True)
    return new_start


def call_context(tx: Tx, tenant: dict, today: dt.date) -> str:
    """What a call should know about routines (and ask, briefly, near the end)."""
    lines = []
    for r in active(tx, tenant["id"]):
        n = day_number(r, today)
        if n is None:
            continue
        items = "; ".join(checklist(r)) or "(no checklist)"
        yday = log_for(tx, tenant["id"], r["id"], today - dt.timedelta(days=1)) if n > 1 else None
        tday = log_for(tx, tenant["id"], r["id"], today)
        status = []
        if n > 1:
            status.append(f"yesterday: {yday['fields'].get('Result') if yday else 'not reported'}")
        status.append(f"today: {tday['fields'].get('Result') if tday else 'not reported yet'}")
        lines.append(f"- {label(r, today)} (started {r['fields'].get('Start Date')}). Daily checklist: {items}. "
                     f"{', '.join(status).capitalize()}.")
    if not lines:
        return ""
    return ("Routines they asked to be checked on:\n" + "\n".join(lines) +
            "\nIf a day isn't reported, ask about it once, briefly, near the end of the call (for a morning call, "
            "yesterday is the day to ask about). Record what they say with log_routine. If they missed something, "
            "don't decide for them: ask whether they want to restart from day 1 or keep going, and pass their "
            "answer as restart.")


def log_from_call(store: Store, tenant: dict, args: dict) -> str:
    """The log_routine tool: {"routine", "day": "today"|"yesterday"|YYYY-MM-DD, "result", "missed", "restart"}."""
    today = briefs.local_now(tenant["timezone"]).date()
    raw_day = str(args.get("day") or "today").lower()
    day = today - dt.timedelta(days=1) if raw_day == "yesterday" else today
    if raw_day not in ("today", "yesterday"):
        try:
            day = dt.date.fromisoformat(raw_day[:10])
        except ValueError:
            pass
    result = str(args.get("result") or "").lower()
    if result not in ("done", "partial", "missed"):
        return "Result must be done, partial or missed."
    with store.tx(tenant["id"]) as tx:
        r = find(tx, tenant["id"], str(args.get("routine") or ""))
        if r is None:
            return "No matching active routine."
        log_day(tx, tenant, r, day, result, str(args.get("missed") or ""), actor="call")
        msg = f"Logged {label(r, day)}: {result}."
        if result != "done" and args.get("restart") is True:
            new = restart(tx, tenant, r, day)
            msg += f" Restarted: day 1 is {new.strftime('%A %-d %B')}."
    return msg


# --- the daily check-in message --------------------------------------------------

def buttons(routine_id, day: dt.date, *pairs: tuple[str, str]) -> list[list[tuple[str, str]]]:
    return [[(text, f"rtn:{choice}:{routine_id}:{day.isoformat()}") for text, choice in pairs]]


def ask_today(tx: Tx, tenant: dict, routine: dict, day: dt.date) -> None:
    items = checklist(routine)
    body = f"{label(routine, day)}. How did today go?" + ("\n" + "\n".join(f"- {i}" for i in items) if items else "")
    tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=body, actor="routines",
                  buttons=buttons(routine["id"], day, ("All done", "done"), ("Missed some", "miss")))


def ask_restart(tx: Tx, tenant: dict, routine: dict, day: dt.date) -> None:
    n = day_number(routine, day)
    tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], actor="routines",
                  body=f"{routine['title']}: day {n} was missed. Restart from day 1 tomorrow, or keep going?",
                  buttons=buttons(routine["id"], day, ("Restart from day 1", "restart"), ("Keep going", "keep")))


def tap(store: Store, tenant: dict | None, chat_id: int, data: str, *, channel: str) -> str:
    """rtn:<done|miss|restart|keep>:<routine id>:<day>"""
    parts = data.split(":")
    if len(parts) != 4 or tenant is None or tenant["status"] != "active":
        return "ignored:routine"
    _, choice, rid, day_s = parts
    try:
        routine_id, day = uuid.UUID(rid), dt.date.fromisoformat(day_s)
    except ValueError:
        return "ignored:routine"
    with store.tx(tenant["id"]) as tx:                 # row-level security: own records only
        routine = tx.record(tenant["id"], routine_id)
        if routine is None or routine["type"] != "routine":
            return "ignored:routine"
        if choice == "done":
            log_day(tx, tenant, routine, day, "done", actor="receiver")
            total, n = length(routine), day_number(routine, day)
            body = f"Logged: {label(routine, day)} done." + (" That's the whole thing. Well done!" if total and n == total else "")
        elif choice == "miss":
            log_day(tx, tenant, routine, day, "missed", actor="receiver")
            ask_restart(tx, tenant, routine, day)
            return "routine:missed"
        elif choice == "restart":
            new = restart(tx, tenant, routine, day)
            body = f"Restarted {routine['title']}: day 1 is {new.strftime('%A %-d %B')}."
        elif choice == "keep":
            body = f"Keeping the count. Tomorrow is day {(day_number(routine, day) or 0) + 1}."
        else:
            return "ignored:routine"
        tx.add_outbox(tenant["id"], chat_id=chat_id, body=body, actor="receiver", channel=channel)
    return f"routine:{choice}"


def tick(store: Store, now: dt.datetime | None = None) -> int:
    """Ask today's check-ins that are due, the restart question after a miss, and finish routines."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with store.tx(system=True) as tx:
        rows = tx._all("SELECT r.*, t.timezone, t.telegram_chat_id, t.schema_name, t.status AS tenant_status "
                       "FROM records r JOIN tenants t ON t.id = r.tenant_id "
                       "WHERE r.type = 'routine' AND coalesce(r.fields->>'Status', 'active') = 'active' "
                       "AND t.status = 'active'")
    sent = 0
    for r in rows:
        tenant = {"id": r["tenant_id"], "timezone": r["timezone"], "telegram_chat_id": r["telegram_chat_id"],
                  "schema_name": r["schema_name"], "status": r["tenant_status"]}
        local = briefs.local_now(r["timezone"], now)
        today = local.date()
        n, total = day_number(r, today), length(r)
        try:
            with store.tx(r["tenant_id"]) as tx:
                if total and n and n > total:
                    tx.update_record(r["tenant_id"], load_schema(r["schema_name"]), r["id"], {"Status": "done"},
                                     actor="routines")
                    tx.add_outbox(r["tenant_id"], chat_id=r["telegram_chat_id"], actor="routines",
                                  body=f"{r['title']} is complete: all {total} days. Well done!")
                    sent += 1
                    continue
                # A miss reported in a message or on a call: ask about restarting, once.
                for day in (today - dt.timedelta(days=1), today):
                    d = log_for(tx, r["tenant_id"], r["id"], day)
                    key = f"routine:restart-asked:{r['id']}:{day.isoformat()}"
                    if d and d["fields"].get("Result") in ("missed", "partial") and not tx.get_state(key) \
                            and d["created_at"] > now - dt.timedelta(days=2):
                        tx.set_state(key, now.isoformat())
                        if d.get("source_inbox_id") is not None:      # buttons already asked
                            ask_restart(tx, tenant, r, day)
                            sent += 1
                if n is None or local.time() < checkin_time(r):
                    continue
                key = f"routine:asked:{r['id']}:{today.isoformat()}"
                if tx.get_state(key) or log_for(tx, r["tenant_id"], r["id"], today):
                    continue
                tx.set_state(key, now.isoformat())
                ask_today(tx, tenant, r, today)
                sent += 1
        except Exception:
            log.exception("routine %s check failed", r["id"])
    return sent


def run(store: Store, settings: Settings) -> None:
    log.info("routines started")
    while True:
        try:
            tick(store)
        except Exception:
            log.exception("routines tick failed")
        time.sleep(CHECK_SECONDS)
