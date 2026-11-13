"""Failure alerts to the owner, on Telegram.

Every few minutes: anything that failed since the last report (messages the
processor gave up on, replies that couldn't be sent, WhatsApp webhook calls
that kept failing), and anything stuck (messages or replies waiting too long).
One message per check, only when there is something to report; each failure
is reported once. Alerts name the user and channel but never the content of
anyone's messages.

Messages held by a spending cap are not failures and are not reported here.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time

from .config import Settings
from .store import Store

log = logging.getLogger("health")

CHECK_EVERY = 300                   # seconds
STUCK_AFTER = dt.timedelta(minutes=10)
STUCK_REMIND = dt.timedelta(hours=1)
MAX_LINES = 8
HELD = "not processed:"             # processor notes of messages held by a cap


def reason(text: str | None) -> str:
    """A short cause, with no message content: the part before any quoted data."""
    t = (text or "").strip()
    t = re.sub(r"^(gave up after \d+ attempts|attempt \d+ failed):\s*", "", t)
    t = re.split(r":\s*['\"{\[]|\s*\(\{|: b?'", t, maxsplit=1)[0]
    return (t[:110] + "…") if len(t) > 110 else (t or "unknown error")


def who(row: dict) -> str:
    name = row.get("display_name") or "user"
    return f"{name} ({str(row['tenant_id'])[:4]})"


def collect(store: Store, now: dt.datetime) -> tuple[list[str], list]:
    """New failures as lines for the alert, and what to mark as reported."""
    lines: list[str] = []
    with store.tx(system=True) as tx:
        inbox = tx._all(
            "SELECT i.id, i.tenant_id, i.source, i.processor_notes, t.display_name FROM inbox i "
            "JOIN tenants t ON t.id = i.tenant_id WHERE i.status = 'failed' AND i.alerted_at IS NULL "
            "AND coalesce(i.processor_notes, '') NOT LIKE %s ORDER BY i.captured_at", (HELD + "%",))
        held = tx._all("SELECT id FROM inbox WHERE status = 'failed' AND alerted_at IS NULL "
                       "AND processor_notes LIKE %s", (HELD + "%",))
        outbox = tx._all(
            "SELECT o.id, o.tenant_id, o.channel, o.error, t.display_name FROM outbox o "
            "JOIN tenants t ON t.id = o.tenant_id WHERE o.status = 'failed' AND o.alerted_at IS NULL "
            "ORDER BY o.created_at")
        events = tx._all("SELECT id, error FROM whatsapp_events WHERE attempts >= 5 AND alerted_at IS NULL")
        waiting = tx._one(
            "SELECT count(*) AS n, min(captured_at) AS oldest FROM inbox WHERE status IN ('pending', 'processing') "
            "AND captured_at < %s", (now - STUCK_AFTER,))
        unsent = tx._one(
            "SELECT count(*) AS n FROM outbox WHERE status IN ('pending', 'sending') AND channel <> 'web' "
            "AND coalesce(scheduled_for, created_at) < %s", (now - STUCK_AFTER,))
        last_stuck = tx.get_state("health:stuck-told")

    if inbox:
        lines.append(f"{len(inbox)} message(s) failed to process (the user was told):")
        lines += [f"- {who(r)} via {r['source']}: {reason(r['processor_notes'])}" for r in inbox[:MAX_LINES]]
    if outbox:
        lines.append(f"{len(outbox)} repl(ies) couldn't be sent:")
        lines += [f"- {who(r)} on {r['channel']}: {reason(r['error'])}" for r in outbox[:MAX_LINES]]
    if events:
        lines.append(f"{len(events)} WhatsApp webhook call(s) failed 5 times and were set aside:")
        lines += [f"- {reason(e['error'])}" for e in events[:3]]
    stuck_parts = []
    if waiting and waiting["n"]:
        mins = int((now - waiting["oldest"]).total_seconds() // 60)
        stuck_parts.append(f"{waiting['n']} message(s) waiting to be processed, oldest {mins} min")
    if unsent and unsent["n"]:
        stuck_parts.append(f"{unsent['n']} repl(ies) waiting to be sent for over 10 min")
    if stuck_parts:
        told = dt.datetime.fromisoformat(last_stuck) if last_stuck else None
        if told is None or now - told > STUCK_REMIND:
            lines.append("Something may be stuck: " + "; ".join(stuck_parts) + ".")
        else:
            stuck_parts = []
    return lines, [("inbox", [r["id"] for r in inbox + held]), ("outbox", [r["id"] for r in outbox]),
                   ("whatsapp_events", [e["id"] for e in events]), ("stuck", bool(stuck_parts))]


def check_once(store: Store, settings: Settings, now: dt.datetime | None = None) -> bool:
    """Report new failures to the owner. True if an alert was queued."""
    now = now or dt.datetime.now(dt.timezone.utc)
    lines, marks = collect(store, now)
    with store.tx(system=True) as tx:
        for table, ids in marks[:3]:
            if ids:
                tx.conn.execute(f"UPDATE {table} SET alerted_at = now() WHERE id = ANY(%s)", (ids,))  # noqa: S608
        if marks[3][1]:
            tx.set_state("health:stuck-told", now.isoformat())
        if not lines:
            return False
        owner = tx.tenant_by_telegram(settings.owner_telegram_user_id) if settings.owner_telegram_user_id else None
        if owner is None:
            log.warning("alert with no owner account: %s", " | ".join(lines))
            return False
        body = "ATYA alert\n\n" + "\n".join(lines) + "\n\nDetails: Messages page (filter: Failed) or the server logs."
        oid = tx.add_outbox(owner["id"], chat_id=owner["telegram_chat_id"], body=body, priority="alert",
                            actor="health", channel="telegram")
        tx.conn.execute("UPDATE outbox SET alerted_at = now() WHERE id = %s", (oid,))  # its own failure isn't re-alerted
    log.warning("alert sent: %s", " | ".join(lines))
    return True


def run(store: Store, settings: Settings) -> None:
    log.info("health checks started (every %ss)", CHECK_EVERY)
    while True:
        try:
            check_once(store, settings)
        except Exception:
            log.exception("health check failed")
        time.sleep(CHECK_EVERY)
