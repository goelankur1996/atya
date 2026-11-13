"""Milestone 5: invites, onboarding, /help, strangers, /export, /delete."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from atya import commands, processor, receiver, sender
from atya.schema import load_schema
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram, run_all

S = load_schema("default")
FRIEND = 888_000_001
STRANGER = 888_000_002


@pytest.fixture
def settings(db_url):
    from atya.config import Settings
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2)


@pytest.fixture
def clean(store):
    with store.tx(system=True) as tx:
        for uid in (OWNER, FRIEND, STRANGER):
            t = tx.tenant_by_telegram(uid)
            if t:
                tx.delete_tenant(t["id"])
        tx.conn.execute("DELETE FROM service_state")
        tx.conn.execute("DELETE FROM waitlist")
    yield


def poll(store, tg, settings):
    receiver.poll_once(store, tg, settings, timeout=0)


def texts(tg, chat):
    return [t for c, t, _ in tg.sent if c == chat]


def tenant_of(store, uid):
    with store.tx(system=True) as tx:
        return tx.tenant_by_telegram(uid)


def invite_code(store, tg, settings):
    tg.push("/invite")
    poll(store, tg, settings)
    msg = texts(tg, 555)[-1]
    return msg.split("Invite code (one use): ")[1].split()[0]


def test_owner_start_and_invite_flow(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    poll(store, tg, settings)
    assert tenant_of(store, OWNER)["status"] == "active"
    code = invite_code(store, tg, settings)
    assert "t.me/atya_test_bot" in texts(tg, 555)[-1]

    # Friend joins with the code and is asked for a timezone; nothing is stored yet.
    tg.push(f"/start {code}", user_id=FRIEND, chat_id=777)
    poll(store, tg, settings)
    t = tenant_of(store, FRIEND)
    assert t["status"] == "invited" and tg.last_buttons
    tg.push("remember to buy milk", user_id=FRIEND, chat_id=777)
    poll(store, tg, settings)
    with store.tx(t["id"]) as tx:
        assert tx._all("SELECT id FROM inbox") == []

    # Tapping a timezone finishes onboarding.
    tg.push_tap("tz:Europe/London", user_id=FRIEND, chat_id=777)
    poll(store, tg, settings)
    t = tenant_of(store, FRIEND)
    assert t["status"] == "active" and t["timezone"] == "Europe/London"
    assert texts(tg, 777)[-1].startswith("Timezone set to Europe/London")

    # The code is single-use.
    tg.push(f"/start {code}", user_id=STRANGER, chat_id=999)
    poll(store, tg, settings)
    assert tenant_of(store, STRANGER) is None
    assert "already been used" in texts(tg, 999)[-1]


def test_typed_timezone(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    poll(store, tg, settings)
    code = invite_code(store, tg, settings)
    tg.push(f"/start {code}", user_id=FRIEND, chat_id=777)
    tg.push("not a zone", user_id=FRIEND, chat_id=777)
    tg.push("europe/paris", user_id=FRIEND, chat_id=777)
    poll(store, tg, settings)
    assert tenant_of(store, FRIEND)["timezone"] == "Europe/Paris"


def test_stranger_joins_the_waitlist_and_answers_once(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")                                       # the owner, to be told
    for text in ("hello", "/help", "/start", "Planning my week, and birthdays", "anyone there?"):
        tg.push(text, user_id=STRANGER, chat_id=999)
    poll(store, tg, settings)
    assert texts(tg, 999) == [commands.WAITLIST_JOINED.format(n=1), commands.WAITLIST_THANKS.format(n=1)]
    assert tenant_of(store, STRANGER) is None
    with store.tx(system=True) as tx:
        (w,) = tx.waitlist()
        owner = tx.tenant_by_telegram(OWNER)
        note = tx._one("SELECT body FROM outbox WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1",
                       (owner["id"],))["body"]
    assert (w["channel"], w["telegram_user_id"], w["want"], w["source"]) == (
        "telegram", STRANGER, "Planning my week, and birthdays", "chat")
    assert note.startswith("New on the waitlist:") and "#1" in note and "Planning" not in note


def test_waitlist_deep_link_and_let_in(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start waitlist", user_id=STRANGER, chat_id=999)
    poll(store, tg, settings)
    with store.tx(system=True) as tx:
        (w,) = tx.waitlist()
    assert w["source"] == "web" and w["want"] is None
    tenant = commands.admit(store, w["id"])
    assert tenant["status"] == "invited" and tenant["telegram_chat_id"] == 999
    with store.tx(system=True) as tx:
        assert tx.waitlist() == []
        out = tx._one("SELECT body, channel, buttons FROM outbox WHERE tenant_id = %s", (tenant["id"],))
    assert out["body"] == commands.SPOT_OPEN and out["channel"] == "telegram" and out["buttons"]
    assert commands.admit(store, w["id"]) is None             # already let in
    tg.push_tap("tz:Asia/Kolkata", user_id=STRANGER, chat_id=999)
    poll(store, tg, settings)
    assert tenant_of(store, STRANGER)["status"] == "active"


def test_only_owner_can_invite(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    poll(store, tg, settings)
    code = invite_code(store, tg, settings)
    tg.push(f"/start {code}", user_id=FRIEND, chat_id=777)
    tg.push_tap("tz:Asia/Kolkata", user_id=FRIEND, chat_id=777)
    tg.push("/invite", user_id=FRIEND, chat_id=777)
    poll(store, tg, settings)
    assert texts(tg, 777)[-1] == "Only the owner can create invites for now."


def test_help_and_unknown_command(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    tg.push("/help")
    tg.push("/frobnicate")
    poll(store, tg, settings)
    assert "/export" in texts(tg, 555)[-2]
    assert texts(tg, 555)[-1].startswith("I don't know /frobnicate")


def onboard_friend(store, tg, settings):
    tg.push("/start")
    poll(store, tg, settings)
    code = invite_code(store, tg, settings)
    tg.push(f"/start {code}", user_id=FRIEND, chat_id=777)
    tg.push_tap("tz:Asia/Kolkata", user_id=FRIEND, chat_id=777)
    poll(store, tg, settings)
    return tenant_of(store, FRIEND)


def test_export_contains_everything_and_only_mine(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    friend = onboard_friend(store, tg, settings)
    # Owner captures something private.
    llm.plans["Owner's private note"] = {"writes": [{"type": "thought", "title": "Owner secret"}]}
    tg.push("Owner's private note")
    # Friend captures a task and a person.
    msg = "Call Asha about the flat by Friday"
    llm.plans[msg] = {"writes": [
        {"type": "person", "title": "Asha"},
        {"type": "task", "title": "Call Asha about the flat", "fields": {"Due Date": "2026-11-06", "People": ["Asha"]}}]}
    tg.push(msg, user_id=FRIEND, chat_id=777)
    run_all(store, tg, llm, settings)

    tg.push("/export", user_id=FRIEND, chat_id=777)
    poll(store, tg, settings)
    chat, filename, data = tg.documents[-1]
    assert chat == 777 and filename.endswith(".zip")
    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    assert {"README.txt", "raw_inbox.jsonl", "audit_log.jsonl", "schema.yaml",
            "records/task.csv", "records/person.csv"} <= names
    inbox = [json.loads(l) for l in z.read("raw_inbox.jsonl").decode().splitlines()]
    assert [m["raw_content"] for m in inbox] == [msg, "/export"]   # commands are kept too
    task_md = next(n for n in names if n.startswith("markdown/Tasks/TSK-1"))
    md = z.read(task_md).decode()
    assert "[[PER-1 Asha]]" in md and "2026-11-06" in md and "From message: INB-1" in md
    everything = b"".join(z.read(n) for n in names)
    assert b"Owner secret" not in everything and b"Owner's private note" not in everything


def test_delete_needs_code_sends_export_then_erases(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    friend = onboard_friend(store, tg, settings)
    llm.plans["note"] = {"writes": [{"type": "thought", "title": "A note"}]}
    tg.push("note", user_id=FRIEND, chat_id=777)
    run_all(store, tg, llm, settings)

    tg.push("/delete", user_id=FRIEND, chat_id=777)
    poll(store, tg, settings)
    prompt = texts(tg, 777)[-1]
    code = prompt.rsplit("/delete ", 1)[1].strip()
    tg.push("/delete WRONG", user_id=FRIEND, chat_id=777)
    poll(store, tg, settings)
    assert "doesn't match" in texts(tg, 777)[-1] and tenant_of(store, FRIEND) is not None

    docs_before = len(tg.documents)
    tg.push(f"/delete {code.lower()}", user_id=FRIEND, chat_id=777)
    poll(store, tg, settings)
    assert len(tg.documents) == docs_before + 1           # final export went out first
    assert texts(tg, 777)[-1].startswith("Done.")
    assert tenant_of(store, FRIEND) is None
    with store.tx(system=True) as tx:
        for table in ("inbox", "records", "audit_log", "outbox"):
            assert tx._one(f"SELECT count(*) AS n FROM {table} WHERE tenant_id = %s",
                           (friend["id"],))["n"] == 0
    # Owner's account is untouched.
    assert tenant_of(store, OWNER)["status"] == "active"


def test_owner_cannot_delete_from_chat(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    tg.push("/delete")
    poll(store, tg, settings)
    assert texts(tg, 555)[-1] == "The owner account can't be deleted from chat."


def test_commands_and_replies_are_kept_with_secrets_hidden(store, settings, clean):
    from atya.config import Settings
    s2 = Settings(**{**settings.__dict__, "web_base_url": "https://atya.test"})
    tg, llm = FakeTelegram(), FakeLLM()
    tg.push("/start")
    tg.push("/help")
    tg.push("/web")
    run_all(store, tg, llm, s2)
    with store.tx(system=True) as tx:
        t = tx.tenant_by_telegram(OWNER)
        rows = tx._all("SELECT raw_content, intent, status, processor_notes FROM inbox "
                       "WHERE tenant_id = %s ORDER BY seq", (t["id"],))
        sent = tx._all("SELECT body, status FROM outbox WHERE tenant_id = %s ORDER BY created_at", (t["id"],))
    # /start created the account, so it came before there was anything to log against.
    assert [(r["raw_content"], r["intent"], r["status"]) for r in rows] == [
        ("/help", "command", "responded"), ("/web", "command", "responded")]
    assert rows[1]["processor_notes"] == "web:link-sent"
    assert sent[0]["body"].startswith("Send me anything") and sent[0]["status"] == "sent"
    assert "/login/[hidden]" in sent[1]["body"]
    token = tg.sent[-1][1].split("/login/")[1].split()[0]
    assert token not in sent[1]["body"]
    # The same update delivered twice is handled once.
    from atya import receiver
    receiver.handle_update(store, tg, s2, tg.queue[-1])
    with store.tx(system=True) as tx:
        assert tx._one("SELECT count(*) AS n FROM inbox WHERE tenant_id = %s", (t["id"],))["n"] == 2
