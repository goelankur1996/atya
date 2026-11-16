"""Routines (e.g. 75 Hard): daily check-in at a set time, logging by buttons or on a call,
asking (not deciding) whether to restart after a miss, and finishing after the last day."""
import datetime as dt

import pytest

from atya import routines
from atya.schema import load_schema

S = load_schema("default")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def at(day, h, m=0):
    return dt.datetime(2026, 11, day, h, m, tzinfo=IST).astimezone(dt.timezone.utc)


@pytest.fixture
def user(store):
    with store.tx(system=True) as tx:
        t = tx.create_tenant(telegram_user_id=880041, telegram_chat_id=880041, display_name="Ankur",
                             timezone="Asia/Kolkata")
        tx.set_tenant_status(t["id"], "active", actor="test")
        t = tx.tenant(t["id"])
    yield t
    with store.tx(system=True) as tx:
        tx.delete_tenant(t["id"])
        tx.conn.execute("DELETE FROM service_state WHERE key LIKE 'routine:%%'")


@pytest.fixture
def hard(store, user):
    with store.tx(user["id"]) as tx:
        r, _ = tx.create_record(user["id"], S, "routine", title="75 Hard", enforce_verbatim=False, actor="t",
                                fields={"Start Date": "2026-11-15", "Days": 75,
                                        "Checklist": "Two 45-min workouts, one outdoors\nFollow the diet\n"
                                                     "No alcohol\nA gallon of water\nRead 10 pages\nProgress photo"})
    return r


def outbox(store, tid):
    with store.tx(tid) as tx:
        return tx._all("SELECT body, buttons FROM outbox WHERE tenant_id = %s ORDER BY created_at", (tid,))


def test_day_numbers_and_checklist(hard):
    assert routines.day_number(hard, dt.date(2026, 11, 15)) == 1
    assert routines.label(hard, dt.date(2026, 11, 16)) == "75 Hard, day 2 of 75"
    assert routines.checklist(hard)[0] == "Two 45-min workouts, one outdoors" and len(routines.checklist(hard)) == 6
    assert routines.checkin_time(hard) == dt.time(21, 0)


def test_evening_checkin_and_buttons(store, user, hard):
    assert routines.tick(store, at(16, 20, 59)) == 0
    assert routines.tick(store, at(16, 21, 0)) == 1
    assert routines.tick(store, at(16, 21, 5)) == 0                                   # once a day
    msg = outbox(store, user["id"])[-1]
    assert msg["body"].startswith("75 Hard, day 2 of 75. How did today go?") and "Read 10 pages" in msg["body"]
    done = msg["buttons"][0][0][1]
    assert routines.tap(store, user, 880041, done, channel="telegram") == "routine:done"
    with store.tx(user["id"]) as tx:
        d = routines.log_for(tx, user["id"], hard["id"], dt.date(2026, 11, 16))
    assert d["fields"]["Result"] == "done" and d["title"] == "75 Hard, day 2 of 75"


def test_a_miss_asks_and_restart_moves_day_one(store, user, hard):
    routines.tick(store, at(17, 21, 0))
    miss = outbox(store, user["id"])[-1]["buttons"][0][1][1]
    assert routines.tap(store, user, 880041, miss, channel="telegram") == "routine:missed"
    ask = outbox(store, user["id"])[-1]
    assert "day 3 was missed" in ask["body"] and [b[0] for b in ask["buttons"][0]] == ["Restart from day 1",
                                                                                      "Keep going"]
    assert routines.tap(store, user, 880041, ask["buttons"][0][0][1], channel="telegram") == "routine:restart"
    with store.tx(user["id"]) as tx:
        r = tx.record(user["id"], hard["id"])
    assert r["fields"]["Start Date"] == "2026-11-18" and "Restarted on 2026-11-18" in r["fields"]["Notes"]
    assert routines.tick(store, at(17, 22, 0)) == 0                       # the button flow already asked


def test_call_tool_and_context(store, user, hard):
    with store.tx(user["id"]) as tx:
        ctx = routines.call_context(tx, user, dt.date(2026, 11, 16))
    # On a call, check-ins and restarts go to ATYA like a text (the atya tool), not a routine tool of their own.
    assert "75 Hard, day 2 of 75" in ctx and "Yesterday: not reported" in ctx and "atya tool" in ctx


def test_finishes_after_the_last_day(store, user):
    with store.tx(user["id"]) as tx:
        r, _ = tx.create_record(user["id"], S, "routine", title="No sugar", enforce_verbatim=False, actor="t",
                                fields={"Start Date": "2026-11-01", "Days": 10})
    routines.tick(store, at(16, 9, 0))
    assert outbox(store, user["id"])[-1]["body"] == "No sugar is complete: all 10 days. Well done!"
    with store.tx(user["id"]) as tx:
        assert tx.record(user["id"], r["id"])["fields"]["Status"] == "done"


def test_filing_sees_active_routines_and_recent_days(store, user, hard):
    from atya import processor
    with store.tx(user["id"]) as tx:
        routines.log_day(tx, user, hard, dt.date(2026, 11, 15), "done", "", actor="t")
    with store.tx(user["id"]) as tx:
        ctx = processor.fanout_context(tx, user["id"])
    (r,) = ctx["Active routines (recent check-ins: date result (id))"]
    assert r["title"] == "75 Hard" and "started 2026-11-15" in r["note"] and "2026-11-15 done" in r["note"]
