"""End to end: Telegram update -> inbox -> processor -> records / outbox -> sent.

Uses a fake Telegram and a fake LLM with canned plans; the database is real.
"""

from __future__ import annotations

import time

import pytest

from atya import processor, receiver, sender
from atya.config import Settings
from atya.schema import load_schema

S = load_schema("default")
OWNER = 777_000_001


class FakeTelegram:
    def __init__(self) -> None:
        self.queue: list[dict] = []
        self.sent: list[tuple[int, str, int | None]] = []
        self._next = 1000

    def push(self, text: str, user_id: int = OWNER, chat_id: int = 555) -> None:
        self._next += 1
        self.queue.append({"update_id": self._next, "message": {
            "message_id": self._next, "date": int(time.time()), "text": text,
            "from": {"id": user_id, "first_name": "Ankur", "username": "ankur"},
            "chat": {"id": chat_id}}})

    def get_updates(self, offset, timeout=30):
        return [u for u in self.queue if offset is None or u["update_id"] >= offset]

    def send_message(self, chat_id, text, reply_to=None):
        self.sent.append((chat_id, text, reply_to))
        return {}


class FakeLLM:
    def __init__(self) -> None:
        self.intent: dict[str, str] = {}
        self.plans: dict[str, dict] = {}
        self.query_plans: dict[str, dict] = {}
        self.seen_records: list[dict] = []
        self.fail_times = 0

    def classify(self, text):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("model overloaded")
        return self.intent.get(text, "log")

    def plan_fanout(self, *, schema, raw_content, captured_at, context):
        self.last_context = context
        return self.plans[raw_content]

    def plan_query(self, *, schema, question, today):
        return self.query_plans[question]

    def answer(self, *, schema, question, records, today):
        self.seen_records = records
        return "Answer: " + ", ".join(r["id"] for r in records)


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, max_attempts=2)


@pytest.fixture
def clean(store):
    with store.tx() as tx:
        old = tx.tenant_by_telegram(OWNER)
        if old:
            tx.delete_tenant(old["id"])
        tx.set_state(receiver.OFFSET_KEY, None)
    yield


def run_all(store, tg, llm, settings):
    receiver.poll_once(store, tg, settings, timeout=0)
    while processor.process_once(store, llm, settings):
        pass
    sender.send_once(store, tg)


def owner_tenant(store):
    with store.tx() as tx:
        return tx.tenant_by_telegram(OWNER)


def test_unknown_sender_is_dropped(store, settings, clean):
    tg = FakeTelegram()
    tg.push("hi", user_id=123)
    receiver.poll_once(store, tg, settings, timeout=0)
    with store.tx() as tx:
        assert tx.tenant_by_telegram(123) is None


def test_capture_creates_linked_records(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    msg = "Met Rahul from Addverb. Need to send him the DROID audit by Friday."
    llm.plans[msg] = {"writes": [
        {"type": "task", "title": "Send Rahul the DROID audit",
         "fields": {"Status": "next", "Priority": "P1", "Due Date": "2026-11-06", "People": ["Rahul"]}},
        {"type": "person", "title": "Rahul", "fields": {"Company": "Addverb", "Relationship": "contact"}},
    ], "processor_notes": "task + person"}
    tg.push(msg)
    run_all(store, tg, llm, settings)

    t = owner_tenant(store)
    with store.tx() as tx:
        inbox = tx._all("SELECT * FROM inbox WHERE tenant_id = %s", (t["id"],))
        assert [r["status"] for r in inbox] == ["processed"] and inbox[0]["intent"] == "log"
        assert "TSK-1" in inbox[0]["processor_notes"] and "PER-1" in inbox[0]["processor_notes"]
        task = tx.list_records(t["id"], "task")[0]
        person = tx.list_records(t["id"], "person")[0]
        assert [l["to_id"] for l in tx.links_from(task["id"])] == [person["id"]]
        assert task["source_inbox_id"] == inbox[0]["id"]


def test_existing_person_is_updated_not_duplicated(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    a, b = "Coffee with Meera, PM at Zeta.", "Meera moved to Stripe."
    llm.plans[a] = {"writes": [{"type": "person", "title": "Meera",
                                "fields": {"Company": "Zeta", "Context": "PM at Zeta"}}]}
    llm.plans[b] = {"writes": [{"type": "person", "title": "meera",
                                "fields": {"Company": "Stripe", "Context": "moved to Stripe"}}]}
    tg.push(a)
    run_all(store, tg, llm, settings)
    tg.push(b)
    run_all(store, tg, llm, settings)
    t = owner_tenant(store)
    with store.tx() as tx:
        people = tx.list_records(t["id"], "person")
        assert len(people) == 1
        p = people[0]
        assert p["fields"]["Company"] == "Stripe"
        assert p["fields"]["Context"] == "PM at Zeta\nmoved to Stripe"
        assert "Last Contact" in p["fields"]


def test_thought_body_is_verbatim_and_child_links_parent(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = None
    with store.tx() as tx:  # owner tenant + the child's parent project
        t = tx.create_tenant(telegram_user_id=OWNER, telegram_chat_id=555)
        tx.create_record(t["id"], S, "project", title="10-Pullup Goal", fields={"Status": "active"})
    msg = "Did 3 pullups today!! Felt strong, grip held."
    llm.plans[msg] = {"writes": [
        {"type": "thought", "title": "Pullup progress", "fields": {"Mood": "energized"},
         "body": "summarised version that must be ignored"},
        {"type": "Fitness — Training Log", "title": "Pullup session",
         "fields": {"Pullups (max)": 3, "How It Felt": "Moderate", "Checkpoint": 5}},
    ]}
    tg.push(msg)
    run_all(store, tg, llm, settings)
    with store.tx() as tx:
        thought = tx.list_records(t["id"], "thought")[0]
        assert thought["body"] == msg
        fit = tx.list_records(t["id"], "fitness")[0]
        assert fit["fields"]["Pullups (max)"] == 3.0
        proj = tx.list_records(t["id"], "project")[0]
        assert [l["to_id"] for l in tx.links_from(fit["id"])] == [proj["id"]]


def test_question_is_answered_from_records(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    cap = "Decided to use Voyage for embeddings because Anthropic points to it."
    llm.plans[cap] = {"writes": [{"type": "decision", "title": "Use Voyage for embeddings",
                                  "fields": {"Reasoning": "Anthropic points to it"}}]}
    q = "What did I decide about embeddings?"
    llm.intent[q] = "query"
    llm.query_plans[q] = {"types": ["decision"], "search": "embeddings"}
    tg.push(cap)
    tg.push(q)
    run_all(store, tg, llm, settings)

    assert [r["id"] for r in llm.seen_records] == ["DEC-1"]
    chat_id, text, reply_to = tg.sent[-1]
    assert chat_id == 555 and text == "Answer: DEC-1" and reply_to is not None
    with store.tx() as tx:
        t = tx.tenant_by_telegram(OWNER)
        statuses = {r["raw_content"]: r["status"] for r in
                    tx._all("SELECT raw_content, status FROM inbox WHERE tenant_id = %s", (t["id"],))}
        assert statuses[q] == "responded"
        assert tx._one("SELECT status FROM outbox WHERE tenant_id = %s", (t["id"],))["status"] == "sent"


def test_failure_retries_then_gives_up_and_writes_nothing(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    msg = "Write me down"
    llm.plans[msg] = {"writes": [{"type": "task", "title": "x"}]}
    llm.fail_times = 5
    tg.push(msg)
    receiver.poll_once(store, tg, settings, timeout=0)
    processor.process_once(store, llm, settings)          # attempt 1 -> back to pending
    t = owner_tenant(store)
    with store.tx() as tx:
        row = tx._one("SELECT * FROM inbox WHERE tenant_id = %s", (t["id"],))
        assert row["status"] == "pending" and "attempt 1 failed" in row["processor_notes"]
    processor.process_once(store, llm, settings)          # attempt 2 = max -> failed
    with store.tx() as tx:
        row = tx._one("SELECT * FROM inbox WHERE tenant_id = %s", (t["id"],))
        assert row["status"] == "failed"
        assert tx.list_records(t["id"], "task") == []


def test_receiver_offset_and_dedup(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    tg.push("one")
    receiver.poll_once(store, tg, settings, timeout=0)
    with store.tx() as tx:
        off = tx.get_state(receiver.OFFSET_KEY)
    assert off == tg.queue[-1]["update_id"] + 1
    # Re-delivering the same update (e.g. offset lost) does not duplicate.
    with store.tx() as tx:
        tx.set_state(receiver.OFFSET_KEY, None)
    receiver.poll_once(store, tg, settings, timeout=0)
    t = owner_tenant(store)
    with store.tx() as tx:
        assert tx._one("SELECT count(*) AS n FROM inbox WHERE tenant_id = %s", (t["id"],))["n"] == 1


def test_split_long_message():
    from atya.telegram import split_message
    parts = split_message(("para " * 300 + "\n\n") * 5, limit=4096)
    assert all(len(p) <= 4096 for p in parts) and len(parts) >= 2
