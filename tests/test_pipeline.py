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
        self.documents: list[tuple[int, str, bytes]] = []
        self._next = 1000

    def push(self, text: str, user_id: int = OWNER, chat_id: int = 555) -> None:
        self._next += 1
        self.queue.append({"update_id": self._next, "message": {
            "message_id": self._next, "date": int(time.time()), "text": text,
            "from": {"id": user_id, "first_name": "Ankur", "username": "ankur"},
            "chat": {"id": chat_id}}})

    def get_updates(self, offset, timeout=30):
        return [u for u in self.queue if offset is None or u["update_id"] >= offset]

    def send_message(self, chat_id, text, reply_to=None, buttons=None, preview=True):
        self.sent.append((chat_id, text, reply_to))
        self.last_buttons = buttons
        self.last_preview = preview
        return {}

    def send_document(self, chat_id, filename, data, caption=""):
        self.documents.append((chat_id, filename, data))
        return {}

    def answer_callback(self, callback_id, text=""):
        pass

    def bot_username(self):
        return "atya_test_bot"

    def push_tap(self, data, user_id=OWNER, chat_id=555):
        self._next += 1
        self.queue.append({"update_id": self._next, "callback_query": {
            "id": str(self._next), "data": data, "from": {"id": user_id},
            "message": {"chat": {"id": chat_id}}}})


class FakeLLM:
    def __init__(self) -> None:
        self.intent: dict[str, str] = {}
        self.plans: dict[str, dict] = {}
        self.query_plans: dict[str, dict] = {}
        self.seen_records: list[dict] = []
        self.broader: dict[str, str] = {}
        self.found: dict[str, bool] = {}
        self.broaden_calls: list[tuple] = []
        self.fail_times = 0

    def classify(self, text):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("model overloaded")
        intent = self.intent.get(text, "log")
        return {"intent": intent, "reply": "Hi there." if intent == "chat" else ""}

    def plan_fanout(self, *, schema, raw_content, captured_local, context, hint=None):
        self.last_hint = hint
        self.last_context = context
        self.last_captured_local = captured_local
        return self.plans[raw_content]

    def plan_query(self, *, schema, question, now):
        self.last_now = now
        return self.query_plans[question]

    def broaden_search(self, *, question, tried, found):
        self.broaden_calls.append((question, tried, found))
        return self.broader.get(question, "")

    def answer(self, *, schema, question, records, now):
        self.seen_records = records
        found = self.found.get(question, bool(records))
        return {"text": "Answer: " + ", ".join(r["id"] for r in records), "found": found}


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2)


@pytest.fixture
def clean(store):
    with store.tx(system=True) as tx:
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
    with store.tx(system=True) as tx:
        return tx.tenant_by_telegram(OWNER)


def test_unknown_sender_is_not_stored(store, settings, clean):
    tg = FakeTelegram()
    tg.push("hi", user_id=123)
    receiver.poll_once(store, tg, settings, timeout=0)
    with store.tx(system=True) as tx:
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
    with store.tx(system=True) as tx:
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
    with store.tx(system=True) as tx:
        people = tx.list_records(t["id"], "person")
        assert len(people) == 1
        p = people[0]
        assert p["fields"]["Company"] == "Stripe"
        assert p["fields"]["Context"] == "PM at Zeta\nmoved to Stripe"
        assert "Last Contact" in p["fields"]


def test_thought_body_is_verbatim_and_child_links_parent(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = None
    with store.tx(system=True) as tx:  # owner tenant + the child's parent project
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
    with store.tx(system=True) as tx:
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
    with store.tx(system=True) as tx:
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
    with store.tx(system=True) as tx:
        row = tx._one("SELECT * FROM inbox WHERE tenant_id = %s", (t["id"],))
        assert row["status"] == "pending" and "attempt 1 failed" in row["processor_notes"]
    processor.process_once(store, llm, settings)          # attempt 2 = max -> failed
    with store.tx(system=True) as tx:
        row = tx._one("SELECT * FROM inbox WHERE tenant_id = %s", (t["id"],))
        assert row["status"] == "failed"
        assert tx.list_records(t["id"], "task") == []


def test_receiver_offset_and_dedup(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    tg.push("one")
    receiver.poll_once(store, tg, settings, timeout=0)
    with store.tx(system=True) as tx:
        off = tx.get_state(receiver.OFFSET_KEY)
    assert off == tg.queue[-1]["update_id"] + 1
    # Re-delivering the same update (e.g. offset lost) does not duplicate.
    with store.tx(system=True) as tx:
        tx.set_state(receiver.OFFSET_KEY, None)
    receiver.poll_once(store, tg, settings, timeout=0)
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        assert tx._one("SELECT count(*) AS n FROM inbox WHERE tenant_id = %s", (t["id"],))["n"] == 1


def test_split_long_message():
    from atya.telegram import split_message
    parts = split_message(("para " * 300 + "\n\n") * 5, limit=4096)
    assert all(len(p) <= 4096 for p in parts) and len(parts) >= 2


def test_chat_gets_a_reply_and_saves_nothing(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    llm.intent["hello"] = "chat"
    tg.push("hello")
    run_all(store, tg, llm, settings)
    assert tg.sent[-1][1] == "Hi there."
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        row = tx._one("SELECT status, intent FROM inbox WHERE tenant_id = %s", (t["id"],))
        assert row == {"status": "responded", "intent": "chat"}
        assert tx._one("SELECT count(*) AS n FROM records WHERE tenant_id = %s", (t["id"],))["n"] == 0


def test_model_sees_local_time_with_weekday(store, settings, clean):
    import datetime as dt
    from atya.processor import local_label
    from zoneinfo import ZoneInfo
    # 2026-11-02 19:00 UTC is Tuesday 00:30 in India.
    utc = dt.datetime(2026, 11, 2, 19, 0, tzinfo=dt.timezone.utc)
    assert local_label(utc, ZoneInfo("Asia/Kolkata")) == "Tuesday 2026-11-03 00:30 (Asia/Kolkata)"

    tg, llm = FakeTelegram(), FakeLLM()
    llm.plans["note"] = {"writes": []}
    tg.push("note")
    run_all(store, tg, llm, settings)
    assert llm.last_captured_local.endswith("(Asia/Kolkata)")
    assert owner_tenant(store)["timezone"] == "Asia/Kolkata"


def test_owner_timezone_sync(store, settings, clean):
    from atya.__main__ import sync_owner_timezone
    with store.tx(system=True) as tx:
        tx.create_tenant(telegram_user_id=OWNER, telegram_chat_id=555)   # created as UTC
    sync_owner_timezone(store, settings)
    assert owner_tenant(store)["timezone"] == "Asia/Kolkata"


def test_capture_can_close_an_existing_task(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    a, b = "Remind me to call Meera on Friday.", "Spoke to Meera already. Meeting her and David on 11th."
    llm.plans[a] = {"writes": [{"type": "task", "title": "Call Meera",
                                "fields": {"Status": "next", "Due Date": "2026-11-06"}}]}
    tg.push(a)
    run_all(store, tg, llm, settings)
    with store.tx(system=True) as tx:
        t = tx.tenant_by_telegram(OWNER)
        task_id = tx.list_records(t["id"], "task")[0]["id"]
    llm.plans[b] = {"updates": [{"id": str(task_id), "fields": {"Status": "done", "Completed At": "2026-11-03"}}],
                    "writes": [{"type": "task", "title": "Meeting with Meera and David",
                                "fields": {"Due Date": "2026-11-11"}}]}
    tg.push(b)
    run_all(store, tg, llm, settings)
    ctx = {r["title"]: r["note"] for r in llm.last_context["Open tasks"]}
    assert ctx["Call Meera"] == "TSK-1; next; due 2026-11-06 (Friday)"
    with store.tx(system=True) as tx:
        tasks = {r["title"]: r for r in tx.list_records(t["id"], "task")}
        assert tasks["Call Meera"]["fields"]["Status"] == "done"
        assert tasks["Call Meera"]["fields"]["Completed At"] == "2026-11-03"
        assert len(tasks) == 2
        notes = tx._one("SELECT processor_notes FROM inbox WHERE raw_content = %s AND tenant_id = %s",
                        (b, t["id"]))["processor_notes"]
        assert "TSK-1 (updated)" in notes and "TSK-2" in notes


def test_update_respects_user_edits_and_bad_ids(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    with store.tx(system=True) as tx:
        t = tx.create_tenant(telegram_user_id=OWNER, telegram_chat_id=555)
        task, _ = tx.create_record(t["id"], S, "task", title="Ship v2", fields={"Status": "next"})
        tx.update_record(t["id"], S, task["id"], {"Status": "waiting"}, by_user=True)
    llm.plans["x"] = {"updates": [{"id": str(task["id"]), "fields": {"Status": "done"}},
                                  {"id": "TSK-9", "fields": {"Status": "done"}}]}
    tg.push("x")
    run_all(store, tg, llm, settings)
    with store.tx(system=True) as tx:
        assert tx.record(t["id"], task["id"])["fields"]["Status"] == "waiting"
        notes = tx._one("SELECT processor_notes FROM inbox WHERE tenant_id = %s", (t["id"],))["processor_notes"]
        assert "edited by user" in notes and "not a record ID" in notes


def test_answer_records_carry_weekdays(store, tenant):
    from atya import processor
    with store.tx(system=True) as tx:
        tx.create_record(tenant["id"], S, "task", title="Meeting with Meera and David",
                         fields={"Due Date": "2026-11-11"})
        recs = processor.gather_records(tx, S, tenant["id"], {"types": ["task"], "search": "Meera"})
    assert recs[0]["fields"]["Due Date"] == "2026-11-11 (Wednesday)"
    assert recs[0]["created"].endswith("day)")


def test_thin_search_retries_with_broader_words_across_types(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    cap = "Leg day, squats 5x5 at 80kg"
    llm.plans[cap] = {"writes": [{"type": "journal", "title": "Leg day workout"}]}
    q = "When did I last go to the gym?"
    llm.intent[q] = "query"
    llm.query_plans[q] = {"types": ["task"], "search": "gym"}
    llm.broader[q] = "workout squats training"
    tg.push(cap)
    tg.push(q)
    run_all(store, tg, llm, settings)
    assert llm.broaden_calls == [(q, "gym", [])]
    assert [r["title"] for r in llm.seen_records] == ["Leg day workout"]
    with store.tx(system=True) as tx:
        t = tx.tenant_by_telegram(OWNER)
        notes = tx._one("SELECT processor_notes FROM inbox WHERE raw_content = %s AND tenant_id = %s",
                        (q, t["id"]))["processor_notes"]
    assert "then all types for 'workout squats training'" in notes


def test_no_retry_when_enough_hits_or_no_keywords(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    caps = [f"Gym session {i}" for i in range(6)]
    for c in caps:
        llm.plans[c] = {"writes": [{"type": "journal", "title": c}]}
        tg.push(c)
    q1, q2 = "gym history?", "what's new?"
    for q in (q1, q2):
        llm.intent[q] = "query"
    llm.query_plans[q1] = {"types": ["journal"], "search": "gym"}
    llm.query_plans[q2] = {"types": ["journal"], "search": ""}
    tg.push(q1)
    tg.push(q2)
    run_all(store, tg, llm, settings)
    assert llm.broaden_calls == []


def test_failed_retry_still_answers(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    q = "anything about sailing?"
    llm.intent[q] = "query"
    llm.query_plans[q] = {"types": ["thought"], "search": "sailing"}

    def boom(**kw):
        raise RuntimeError("overloaded")
    llm.broaden_search = boom
    tg.push(q)
    run_all(store, tg, llm, settings)
    assert tg.sent[-1][1] == "Answer: "


# --- relevance and "Save as task" ------------------------------------------------

def test_parse_answer():
    from atya.llm import parse_answer
    assert parse_answer('{"found": false, "answer": "Nothing on that yet."}', had_records=True) == \
        {"text": "Nothing on that yet.", "found": False}
    assert parse_answer('{"found": true, "answer": "[TSK-1] Call Meera"}', had_records=True)["found"] is True
    assert parse_answer('{"found": true, "answer": "x"}', had_records=False)["found"] is False   # no records
    assert parse_answer("plain text answer", had_records=True) == {"text": "plain text answer", "found": True}
    assert parse_answer("", had_records=False) == {"text": "(no answer)", "found": False}


def test_prompts_carry_the_new_rules():
    from atya import llm as L
    assert "check on the Costa Rica product" in L.CLASSIFY_SYSTEM
    assert "Never add topics" in L.BROADEN_SYSTEM
    assert "Never present an unrelated record" in L.ANSWER_SYSTEM
    L.ANSWER_SYSTEM.format(hard_rules="", vocabularies="", lenses="", now="now")   # braces escaped


def test_nothing_found_offers_save_as_task_and_tap_files_it(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    q = "Check on costa rica product"
    llm.intent[q] = "query"
    llm.query_plans[q] = {"types": ["task"], "search": "costa rica product"}
    llm.found[q] = False
    tg.push(q)
    run_all(store, tg, llm, settings)
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        row = tx._one("SELECT * FROM inbox WHERE tenant_id = %s AND raw_content = %s", (t["id"], q))
        out = tx._one("SELECT * FROM outbox WHERE inbox_id = %s", (row["id"],))
    assert row["intent"] == "query" and "offered Save as task" in row["processor_notes"]
    assert out["buttons"] == [[["Save as task", f"save:{row['id']}"]]]
    assert tg.last_buttons == [[("Save as task", f"save:{row['id']}")]]

    # Tap it: the same message is filed as a capture, without asking the classifier again.
    llm.intent[q] = "query"                       # would still say query if asked
    llm.plans[q] = {"writes": [{"type": "task", "title": "Check on Costa Rica product"}]}
    tg.push_tap(f"save:{row['id']}")
    run_all(store, tg, llm, settings)
    assert "Save as task" in (llm.last_hint or "")
    with store.tx(system=True) as tx:
        row2 = tx.inbox_row(row["id"])
        task = tx._one("SELECT * FROM records WHERE tenant_id = %s AND type = 'task'", (t["id"],))
        bodies = [o["body"] for o in tx._all("SELECT body FROM outbox WHERE inbox_id = %s ORDER BY created_at", (row["id"],))]
    assert row2["status"] == "processed" and row2["intent"] == "log" and row2["forced_intent"] == "log"
    assert task["title"] == "Check on Costa Rica product" and task["source_inbox_id"] == row["id"]
    assert "Saving it as a task…" in bodies

    # A second tap does nothing.
    tg.push_tap(f"save:{row['id']}")
    run_all(store, tg, llm, settings)
    with store.tx(system=True) as tx:
        assert tx._one("SELECT count(*) AS n FROM records WHERE tenant_id = %s AND type = 'task'", (t["id"],))["n"] == 1


def test_found_answer_has_no_button_and_strangers_cannot_tap(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    cap = "Met Meera about the pilot"
    llm.plans[cap] = {"writes": [{"type": "person", "title": "Meera"}]}
    q = "who is Meera?"
    llm.intent[q] = "query"
    llm.query_plans[q] = {"types": ["person"], "search": "Meera"}
    tg.push(cap)
    tg.push(q)
    run_all(store, tg, llm, settings)
    assert tg.last_buttons is None
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        qrow = tx._one("SELECT id FROM inbox WHERE tenant_id = %s AND raw_content = %s", (t["id"], q))
    # Someone else (no account) tapping a forged button changes nothing.
    tg.push_tap(f"save:{qrow['id']}", user_id=999_000_111)
    tg.push_tap("save:not-a-uuid")
    run_all(store, tg, llm, settings)
    with store.tx(system=True) as tx:
        assert tx.inbox_row(qrow["id"])["forced_intent"] is None
