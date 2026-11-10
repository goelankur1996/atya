"""Morning brief: /brief command, timing, content, exactly-once delivery."""

from __future__ import annotations

import datetime as dt

import pytest

from atya import briefs, commands
from atya.schema import load_schema
from tests.test_pipeline import FakeTelegram

S = load_schema("default")
UTC = dt.timezone.utc


@pytest.fixture
def settings(db_url):
    from atya.config import Settings
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=1)


def ctx_for(store, settings, tenant, tg=None):
    return commands.Ctx(store=store, tg=tg or FakeTelegram(), settings=settings,
                        user_id=tenant["telegram_user_id"], chat_id=tenant["telegram_chat_id"] or 1,
                        name=None, tenant=tenant)


@pytest.mark.parametrize("text,expected", [
    ("9", dt.time(9)), ("9am", dt.time(9)), ("9 AM", dt.time(9)), ("8:30", dt.time(8, 30)),
    ("08:30", dt.time(8, 30)), ("7pm", dt.time(19)), ("7.30 pm", dt.time(19, 30)), ("19:00", dt.time(19)),
    ("12am", dt.time(0)), ("12pm", dt.time(12)), ("0:15", dt.time(0, 15)),
    ("25", None), ("13pm", None), ("9:75", None), ("soon", None), ("", None),
])
def test_parse_time(text, expected):
    assert briefs.parse_time(text) == expected


def test_brief_content(store, tenant):
    today = dt.date(2026, 11, 11)       # a Wednesday
    with store.tx(system=True) as tx:
        tx.create_record(tenant["id"], S, "task", title="Send the audit", fields={"Status": "next", "Due Date": "2026-11-09"})
        tx.create_record(tenant["id"], S, "task", title="Recalibrate camera", fields={"Status": "next", "Due Date": "2026-11-11"})
        tx.create_record(tenant["id"], S, "task", title="Call Meera", fields={"Status": "waiting", "Due Date": "2026-11-13"})
        tx.create_record(tenant["id"], S, "task", title="Done already", fields={"Status": "done", "Due Date": "2026-11-11"})
        tx.create_record(tenant["id"], S, "task", title="Next month", fields={"Status": "next", "Due Date": "2026-12-20"})
        text = briefs.build_brief(tx, S, tenant["id"], today)
    assert text.startswith("Good morning. Wednesday 11 Nov")
    assert "Overdue\n- TSK-1 Send the audit (since Mon)" in text
    assert "Today\n- TSK-2 Recalibrate camera" in text
    assert "This week\n- TSK-3 Call Meera (Fri)" in text
    assert "Done already" not in text and "Next month" not in text
    assert "Waiting on others: 1 task." in text


def test_brief_when_nothing_due(store, tenant):
    with store.tx(system=True) as tx:
        tx.create_record(tenant["id"], S, "task", title="Someday thing", fields={"Status": "next"})
        text = briefs.build_brief(tx, S, tenant["id"], dt.date(2026, 11, 11))
    assert "Nothing due today or this week. 1 open task." in text


def test_brief_command_on_off_status(store, settings, tenant):
    tg = FakeTelegram()
    ctx = ctx_for(store, settings, tenant, tg)
    assert commands.handle(ctx, "/brief") == "brief:status" and "off" in tg.sent[-1][1]
    assert commands.handle(ctx, "/brief soonish") == "brief:bad-time"
    assert commands.handle(ctx, "/brief 9am") == "brief:on"
    assert "9:00 am" in tg.sent[-1][1]
    with store.tx(system=True) as tx:
        assert tx.tenant(tenant["id"])["brief_time"] == dt.time(9)
    assert commands.handle(ctx, "/brief") == "brief:status" and "9:00 am" in tg.sent[-1][1]
    assert commands.handle(ctx, "/brief off") == "brief:off"
    with store.tx(system=True) as tx:
        assert tx.tenant(tenant["id"])["brief_time"] is None
    assert "/brief" in commands.HELP


def test_brief_sent_once_a_day_at_local_time(store, tenant):
    with store.tx(system=True) as tx:
        tx.set_timezone(tenant["id"], "Asia/Kolkata")
        tx.conn.execute("UPDATE tenants SET telegram_chat_id = 4242 WHERE id = %s", (tenant["id"],))
        tx.set_brief(tenant["id"], dt.time(9))
        tx.create_record(tenant["id"], S, "task", title="Due thing", fields={"Status": "next", "Due Date": "2026-11-11"})
    # 08:59 IST = 03:29 UTC: not yet.
    assert briefs.send_due_briefs(store, now=dt.datetime(2026, 11, 11, 3, 29, tzinfo=UTC)) == 0
    # 09:00 IST: sent.
    assert briefs.send_due_briefs(store, now=dt.datetime(2026, 11, 11, 3, 30, tzinfo=UTC)) == 1
    # Later the same day (or a second run): not again.
    assert briefs.send_due_briefs(store, now=dt.datetime(2026, 11, 11, 3, 31, tzinfo=UTC)) == 0
    assert briefs.send_due_briefs(store, now=dt.datetime(2026, 11, 11, 15, 0, tzinfo=UTC)) == 0
    # Next morning: again.
    assert briefs.send_due_briefs(store, now=dt.datetime(2026, 11, 12, 4, 0, tzinfo=UTC)) == 1
    with store.tx(system=True) as tx:
        rows = tx._all("SELECT chat_id, body, status FROM outbox WHERE tenant_id = %s ORDER BY created_at",
                       (tenant["id"],))
    assert len(rows) == 2 and rows[0]["chat_id"] == 4242 and rows[0]["status"] == "pending"
    assert "Today\n- TSK-1 Due thing" in rows[0]["body"]
    assert "Overdue\n- TSK-1 Due thing (since yesterday)" in rows[1]["body"]


def test_turning_on_after_the_time_starts_tomorrow(store, settings, tenant):
    with store.tx(system=True) as tx:
        tx.conn.execute("UPDATE tenants SET telegram_chat_id = 4242, timezone = 'UTC' WHERE id = %s", (tenant["id"],))
        t = tx.tenant(tenant["id"])
    ctx = ctx_for(store, settings, t)
    now = dt.datetime.now(UTC)
    earlier = (now - dt.timedelta(minutes=5)).time().replace(second=0, microsecond=0)
    if earlier > now.time():               # just after midnight: nothing to test
        pytest.skip("too close to midnight")
    commands.handle(ctx, f"/brief {earlier.strftime('%H:%M')}")
    assert "First one tomorrow" in ctx.tg.sent[-1][1]
    assert briefs.send_due_briefs(store) == 0


def test_inactive_or_off_users_get_nothing(store, tenant):
    with store.tx(system=True) as tx:
        tx.conn.execute("UPDATE tenants SET telegram_chat_id = 1 WHERE id = %s", (tenant["id"],))
        tx.set_brief(tenant["id"], dt.time(0, 0))
        tx.set_tenant_status(tenant["id"], "suspended")
    assert briefs.send_due_briefs(store) == 0
    with store.tx(system=True) as tx:
        tx.set_tenant_status(tenant["id"], "active")
        tx.set_brief(tenant["id"], None)
    assert briefs.send_due_briefs(store) == 0
