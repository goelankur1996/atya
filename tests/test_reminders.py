"""Timed reminders: confirmation, firing once at local time, buttons, edits."""

from __future__ import annotations

import datetime as dt

import pytest

from atya import reminders
from atya.schema import load_schema
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram, owner_tenant, run_all

S = load_schema("default")
UTC = dt.timezone.utc
IST = reminders.tzinfo("Asia/Kolkata")


@pytest.fixture
def settings(db_url):
    from atya.config import Settings
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata")


@pytest.fixture
def clean(store):
    with store.tx(system=True) as tx:
        t = tx.tenant_by_telegram(OWNER)
        if t:
            tx.delete_tenant(t["id"])
        tx.conn.execute("DELETE FROM service_state")
    yield


def test_parse_and_human():
    at = reminders.parse_at("2026-11-11T14:00:00", IST)
    assert at == dt.datetime(2026, 11, 11, 14, 0, tzinfo=IST)
    assert reminders.parse_at("2026-11-11T08:30:00+00:00", IST).astimezone(IST).hour == 14
    assert reminders.parse_at("2026-11-12", IST).hour == 9
    assert reminders.parse_at("soon", IST) is None
    now = dt.datetime(2026, 11, 11, 10, 0, tzinfo=IST)
    assert reminders.human(at, now) == "2:00 pm today"
    assert reminders.human(at + dt.timedelta(days=1), now) == "2:00 pm tomorrow"
    assert reminders.human(at + dt.timedelta(days=3), now) == "2:00 pm Sat 14 Nov"
    assert reminders.snooze_value("1h", IST, now.astimezone(UTC)) == "2026-11-11T11:00:00"
    assert reminders.snooze_value("tmrw", IST, now.astimezone(UTC)) == "2026-11-12T09:00:00"


def _setup(store, settings, llm, tg, remind_at="2026-11-11T14:00"):
    tg.push("hi")
    llm.intent["hi"] = "chat"
    run_all(store, tg, llm, settings)
    msg = "Remind me to book a call with Jim at 2:00 pm"
    llm.intent[msg] = "log"
    llm.plans[msg] = {"writes": [{"type": "task", "title": "Book a call with Jim",
                                  "fields": {"Remind At": remind_at, "Due Date": remind_at[:10]}}]}
    tg.push(msg)
    run_all(store, tg, llm, settings)
    return owner_tenant(store)


def test_capture_confirms_reminder_time(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    future = (dt.datetime.now(IST) + dt.timedelta(days=2)).replace(hour=14, minute=0)
    t = _setup(store, settings, llm, tg, future.strftime("%Y-%m-%dT%H:%M"))
    assert tg.sent[-1][1].startswith("Got it. I'll remind you at 2:00 pm")
    assert "Book a call with Jim (TSK-1)" in tg.sent[-1][1]


def test_reminder_fires_once_at_local_time_with_buttons(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = _setup(store, settings, llm, tg)
    with store.tx(system=True) as tx:
        tx.conn.execute("UPDATE tenants SET telegram_chat_id = 555 WHERE id = %s", (t["id"],))
    before = dt.datetime(2026, 11, 11, 8, 29, tzinfo=UTC)       # 13:59 IST
    on_time = dt.datetime(2026, 11, 11, 8, 30, 20, tzinfo=UTC)  # 14:00 IST
    assert reminders.send_due_reminders(store, now=before) == 0
    assert reminders.send_due_reminders(store, now=on_time) == 1
    assert reminders.send_due_reminders(store, now=on_time + dt.timedelta(minutes=1)) == 0
    with store.tx(system=True) as tx:
        out = tx._one("SELECT * FROM outbox WHERE tenant_id = %s AND priority = 'alert'", (t["id"],))
        task = tx._one("SELECT id FROM records WHERE tenant_id = %s AND type = 'task'", (t["id"],))
    assert out["body"] == "⏰ Book a call with Jim (TSK-1)" and out["chat_id"] == 555
    assert out["buttons"][0][0] == ["Done", f"rem:done:{task['id']}"]

    # Snooze an hour: the field changes, so it fires again later.
    tg.push_tap(f"rem:1h:{task['id']}")
    run_all(store, tg, llm, settings)
    assert "I'll remind you again at" in tg.sent[-1][1]
    with store.tx(system=True) as tx:
        rec = tx.record(t["id"], task["id"])
    assert "Remind At" in rec["user_edited_fields"]
    later = reminders.parse_at(rec["fields"]["Remind At"], IST)
    assert reminders.send_due_reminders(store, now=later.astimezone(UTC) - dt.timedelta(minutes=1)) == 0
    assert reminders.send_due_reminders(store, now=later.astimezone(UTC) + dt.timedelta(seconds=30)) == 1

    # Done: task closes, no more reminders.
    tg.push_tap(f"rem:done:{task['id']}")
    run_all(store, tg, llm, settings)
    with store.tx(system=True) as tx:
        rec = tx.record(t["id"], task["id"])
    assert rec["fields"]["Status"] == "done" and rec["fields"]["Completed At"]
    assert tg.sent[-1][1] == "Done: Book a call with Jim (TSK-1)"
    with store.tx(system=True) as tx:
        tx.update_record(t["id"], S, task["id"], {"Remind At": "2026-11-11T15:00"}, by_user=True)
    assert reminders.send_due_reminders(store, now=dt.datetime(2026, 11, 12, tzinfo=UTC)) == 0


def test_late_reminder_says_when_it_was_due_and_old_ones_dropped(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = _setup(store, settings, llm, tg)
    with store.tx(system=True) as tx:
        tx.conn.execute("UPDATE tenants SET telegram_chat_id = 555 WHERE id = %s", (t["id"],))
    assert reminders.send_due_reminders(store, now=dt.datetime(2026, 11, 20, tzinfo=UTC)) == 0  # > 7 days
    assert reminders.send_due_reminders(store, now=dt.datetime(2026, 11, 11, 11, 0, tzinfo=UTC)) == 1
    with store.tx(system=True) as tx:
        body = tx._one("SELECT body FROM outbox WHERE tenant_id = %s AND priority = 'alert'", (t["id"],))["body"]
    assert "(was due 2:00 pm today)" in body


def test_buttons_only_work_for_the_owner_of_the_task(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = _setup(store, settings, llm, tg)
    with store.tx(system=True) as tx:
        task = tx._one("SELECT id FROM records WHERE tenant_id = %s AND type = 'task'", (t["id"],))
        other = tx.create_tenant(telegram_user_id=999_777_111, telegram_chat_id=1, display_name="x")
    try:
        tg.push_tap(f"rem:done:{task['id']}", user_id=999_777_111)
        tg.push_tap("rem:done:not-a-uuid")
        tg.push_tap(f"rem:explode:{task['id']}")
        run_all(store, tg, llm, settings)
        with store.tx(system=True) as tx:
            assert tx.record(t["id"], task["id"])["fields"].get("Status") != "done"
    finally:
        with store.tx(system=True) as tx:
            tx.delete_tenant(other["id"])


def test_remind_at_shown_and_editable_on_web(store, settings, tenant, db_url):
    from atya import web
    from tests.test_web import page_form, sign_in
    settings = settings.__class__(**{**settings.__dict__, "web_base_url": "https://atya.test"})
    app = web.create_app(store, settings)
    with store.tx(system=True) as tx:
        tx.create_record(tenant["id"], S, "task", title="Call Jim", fields={"Remind At": "2026-11-11T14:00"})
    client, _ = sign_in(app, store, settings, tenant)
    page = client.get("/r/TSK-1").get_data(as_text=True)
    assert 'type="datetime-local"' in page and 'value="2026-11-11T14:00"' in page
    r = client.post("/r/TSK-1", data={**page_form(client, "/r/TSK-1"), "title": "Call Jim",
                                      "f:Remind At": "2026-11-11T14:00"})
    with store.tx(system=True) as tx:
        assert tx.record_by_ref(tenant["id"], "task", 1)["user_edited_fields"] == []   # untouched
    client.post("/r/TSK-1", data={**page_form(client, "/r/TSK-1"), "title": "Call Jim",
                                  "f:Remind At": "2026-11-11T16:30"})
    with store.tx(system=True) as tx:
        assert tx.record_by_ref(tenant["id"], "task", 1)["fields"]["Remind At"] == "2026-11-11T16:30:00"
