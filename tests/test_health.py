"""Failure alerts: the user hears about a failure, the owner gets one alert per failure, no content leaks."""

from __future__ import annotations

import datetime as dt

import pytest

from atya import health, processor, receiver, sender
from atya.config import Settings
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram

FRIEND = 880002


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2)


@pytest.fixture
def clean(store):
    with store.tx(system=True) as tx:
        for t in tx._all("SELECT id FROM tenants WHERE telegram_user_id = ANY(%s)", ([OWNER, FRIEND],)):
            tx.delete_tenant(t["id"])
        tx.conn.execute("DELETE FROM service_state")
        # Other tests' leftovers must not show up in these alerts.
        tx.conn.execute("UPDATE inbox SET alerted_at = now() WHERE alerted_at IS NULL")
        tx.conn.execute("UPDATE inbox SET status = 'processed' WHERE status IN ('pending', 'processing')")
        tx.conn.execute("UPDATE outbox SET alerted_at = now() WHERE alerted_at IS NULL")
        tx.conn.execute("UPDATE outbox SET status = 'failed' WHERE status IN ('pending', 'sending')")
        tx.conn.execute("DELETE FROM whatsapp_events")
    yield


class BrokenLLM(FakeLLM):
    def classify(self, text):
        raise RuntimeError("non-JSON response: 'my secret plans for Tanvi'")


def to(tg, chat):
    return [m[1] for m in tg.sent if m[0] == chat]


def test_failure_tells_user_and_alerts_owner_once_without_content(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    receiver.poll_once(store, tg, settings, timeout=0)
    with store.tx(system=True) as tx:
        tx.create_tenant(telegram_user_id=FRIEND, telegram_chat_id=FRIEND, display_name="Ravi", timezone="UTC")
    tg.push("my secret plans for Tanvi", user_id=FRIEND, chat_id=FRIEND)
    receiver.poll_once(store, tg, settings, timeout=0)
    llm = BrokenLLM()
    for _ in range(3):                                   # max_attempts = 2
        processor.process_once(store, llm, settings)
        with store.tx(system=True) as tx:                 # retries wait for their turn; skip the wait
            tx.conn.execute("UPDATE inbox SET claimed_at = now() - interval '1 hour'")
    sender.send_once(store, tg)
    assert to(tg, FRIEND)[-1] == processor.FAILED_REPLY

    assert health.check_once(store, settings) is True
    sender.send_once(store, tg)
    alert = to(tg, 555)[-1]
    assert alert.startswith("ATYA alert") and "1 message(s) failed to process" in alert
    assert "Ravi" in alert and "via telegram" in alert
    assert "Tanvi" not in alert and "secret" not in alert          # never message content
    assert health.check_once(store, settings) is False              # reported once


def test_failed_sends_and_stuck_messages(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    receiver.poll_once(store, tg, settings, timeout=0)
    with store.tx(system=True) as tx:
        f = tx.create_tenant(telegram_user_id=FRIEND, telegram_chat_id=FRIEND, display_name="Ravi", timezone="UTC")
        oid = tx.add_outbox(f["id"], chat_id=FRIEND, body="hello")
        tx.finish_outbox(oid, ok=False, error="Forbidden: bot was blocked by the user")
        tx.add_inbox(f["id"], source="telegram", raw_content="old one",
                     captured_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30))
    assert health.check_once(store, settings)
    sender.send_once(store, tg)
    alert = to(tg, 555)[-1]
    assert "couldn't be sent" in alert and "Forbidden" in alert
    assert "1 message(s) waiting to be processed, oldest 30 min" in alert
    # Still stuck a minute later: not repeated within the hour.
    assert not health.check_once(store, settings, now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1))
    assert health.check_once(store, settings, now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=61))


def test_held_messages_are_not_failures(store, settings, clean):
    tg = FakeTelegram()
    tg.push("/start")
    receiver.poll_once(store, tg, settings, timeout=0)
    with store.tx(system=True) as tx:
        f = tx.create_tenant(telegram_user_id=FRIEND, telegram_chat_id=FRIEND, timezone="UTC")
        iid = tx.add_inbox(f["id"], source="telegram", raw_content="x")
        tx.finish_inbox(iid, status="failed", notes="not processed: daily limit reached (2 messages a day)")
    assert health.check_once(store, settings) is False


def test_reason_strips_content():
    assert health.reason("gave up after 3 attempts: non-JSON response: 'dear diary'") == "non-JSON response"
    assert health.reason("LLMError: no JSON object in response: \"hello\"") == "LLMError: no JSON object in response"
    assert health.reason("Forbidden: bot was blocked by the user") == "Forbidden: bot was blocked by the user"
