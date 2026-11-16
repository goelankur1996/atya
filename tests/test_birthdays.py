"""Birthdays live on people and show in the brief; they are not tasks."""
import datetime as dt

import pytest

from atya import briefs, connections
from atya.schema import load_schema

S = load_schema("default")


@pytest.fixture
def user(store):
    with store.tx(system=True) as tx:
        t = tx.create_tenant(telegram_user_id=880031, telegram_chat_id=880031, display_name="Ira",
                             timezone="Asia/Kolkata")
    yield t
    with store.tx(system=True) as tx:
        tx.delete_tenant(t["id"])


def test_brief_lists_birthdays_today_and_this_week(store, user):
    with store.tx(user["id"]) as tx:
        tx.create_record(user["id"], S, "person", title="Meera", fields={"Birthday": "1990-11-16"},
                         enforce_verbatim=False, actor="test")
        tx.create_record(user["id"], S, "person", title="Rohan", fields={"Birthday": "11-19"},
                         enforce_verbatim=False, actor="test")
        tx.create_record(user["id"], S, "person", title="Later", fields={"Birthday": "12-30"},
                         enforce_verbatim=False, actor="test")
        tx.create_record(user["id"], S, "person", title="Raj", fields={"Anniversary": "2020-11-18"},
                         enforce_verbatim=False, actor="test")
        brief = briefs.build_brief(tx, S, user["id"], dt.date(2026, 11, 16))
    assert "Birthdays\n- Meera, turns 36 (today)\n- Raj (anniversary), 6 years (Wed)\n- Rohan (Thu)" in brief
    assert "Later" not in brief


def test_old_birthday_tasks_are_removed_once(store, user):
    with store.tx(system=True) as tx:
        tx.set_state(connections.CLEANUP_KEY, None)
    with store.tx(user["id"]) as tx:
        tx.create_record(user["id"], S, "task", title="Wish Meera a happy birthday", enforce_verbatim=False,
                         actor="test")
        tx.create_record(user["id"], S, "task", title="Buy a birthday gift for Meera", enforce_verbatim=False,
                         actor="test")
        tx.create_record(user["id"], S, "task", title="x", external_id="gbday:c1:2026", enforce_verbatim=False,
                         actor="google")
    assert connections.remove_birthday_tasks(store) >= 2
    with store.tx(user["id"]) as tx:
        left = [r["title"] for r in tx._all("SELECT title FROM records WHERE tenant_id = %s", (user["id"],))]
    assert left == ["Buy a birthday gift for Meera"]
    assert connections.remove_birthday_tasks(store) == 0                         # only once
