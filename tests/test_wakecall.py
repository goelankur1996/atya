"""The morning call: scheduling, retries, the brief fallback, commands, and saving what was said.
(The call itself needs real Telegram accounts; here it's a stub.)"""
import asyncio
import datetime as dt
import json

import pytest

from atya import processor, receiver, wakecall
from atya.config import Settings
from atya.sonic import Sonic
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram, owner_tenant, run_all


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2,
                    tg_caller_api_id="1", tg_caller_api_hash="h", tg_caller_session="s")


@pytest.fixture
def owner(store, settings):
    with store.tx(system=True) as tx:
        old = tx.tenant_by_telegram(OWNER)
        if old:
            tx.delete_tenant(old["id"])
        tx.conn.execute("DELETE FROM service_state WHERE key LIKE 'wakecall:%%'")
        tx.set_state(receiver.OFFSET_KEY, None)
    tg = FakeTelegram()
    tg.push("/start")
    run_all(store, tg, FakeLLM(), settings)
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        tx.set_timezone(t["id"], "Asia/Kolkata")
    yield owner_tenant(store)
    with store.tx(system=True) as tx:
        tx.conn.execute("DELETE FROM service_state WHERE key LIKE 'wakecall:%%'")


def ist(h, m=0, day=16):
    return dt.datetime(2026, 11, day, h, m, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))) \
        .astimezone(dt.timezone.utc)


class Phone:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def __call__(self, tenant):
        self.calls += 1
        return self.results.pop(0)


def outbox(store, tid):
    with store.tx(tid) as tx:
        return [r["body"] for r in tx._all("SELECT body FROM outbox WHERE tenant_id = %s ORDER BY created_at",
                                           (tid,))]


def test_off_by_default_and_on_request(store, settings, owner):
    phone = Phone("answered")
    assert wakecall.tick(store, settings, phone, now=ist(6, 1)) is None
    with store.tx(system=True) as tx:
        tx.set_state(wakecall.NOW_KEY, "x")
    assert wakecall.tick(store, settings, phone, now=ist(14)) == "on-request:answered"
    assert wakecall.tick(store, settings, phone, now=ist(14)) is None and phone.calls == 1


def test_retries_then_sends_the_brief(store, settings, owner):
    with store.tx(owner["id"]) as tx:
        tx.set_wakecall(owner["id"], dt.time(6, 0))
    phone = Phone("missed", "missed", "missed")
    assert wakecall.tick(store, settings, phone, now=ist(5, 59)) is None
    assert wakecall.tick(store, settings, phone, now=ist(6, 0)) == "missed:1"
    assert wakecall.tick(store, settings, phone, now=ist(6, 5)) is None          # waits 10 minutes
    assert wakecall.tick(store, settings, phone, now=ist(6, 11)) == "missed:2"
    assert wakecall.tick(store, settings, phone, now=ist(6, 22)) == "missed:3"
    assert wakecall.tick(store, settings, phone, now=ist(6, 40)) is None and phone.calls == 3
    assert outbox(store, owner["id"])[-1].startswith(wakecall.MISSED_NOTE)
    phone2 = Phone("answered")
    assert wakecall.tick(store, settings, phone2, now=ist(9)) is None              # past the 2-hour window
    assert wakecall.tick(store, settings, phone2, now=ist(6, 0, day=17)) == "answered:1"
    assert wakecall.tick(store, settings, phone2, now=ist(6, 30, day=17)) is None


def test_commands(store, settings, owner):
    tg = FakeTelegram()
    tg._next = 5000                       # after the fixture's /start
    tg.push("/wakecall 6:30")
    tg.push("/callme")
    run_all(store, tg, FakeLLM(), settings)
    assert owner_tenant(store)["wakecall_time"] == dt.time(6, 30), tg.sent
    with store.tx(system=True) as tx:
        assert tx.get_state(wakecall.NOW_KEY)
    tg.push("/wakecall off")
    run_all(store, tg, FakeLLM(), settings)
    assert owner_tenant(store)["wakecall_time"] is None
    texts = [text for _, text, _ in tg.sent]
    assert any("Calling you" in t for t in texts) and any("turned off" in t for t in texts)


def test_transcript_is_filed_and_summarised(store, settings, owner):
    transcript = [("ASSISTANT", "Good morning Ankur. How did you sleep?"), ("USER", "fine"),
                  ("ASSISTANT", "Anything for today?"), ("USER", "workout at seven and call Rahul about the audit")]
    assert wakecall.save_transcript(store, owner, transcript, dt.datetime.now(dt.timezone.utc))
    assert not wakecall.save_transcript(store, owner, [("ASSISTANT", "Hello?")], dt.datetime.now(dt.timezone.utc))
    raw = "ATYA: Good morning Ankur. How did you sleep?\nAnkur: fine\nATYA: Anything for today?\n" \
          "Ankur: workout at seven and call Rahul about the audit"
    llm = FakeLLM()
    llm.plans[raw] = {"writes": [{"type": "task", "title": "Call Rahul about the audit", "fields": {}}]}
    while processor.process_once(store, llm, settings):
        pass
    assert llm.last_hint == processor.CALL_HINT
    assert "From this morning's call, saved:" in outbox(store, owner["id"])[-1]
    assert "Call Rahul about the audit" in outbox(store, owner["id"])[-1]


def test_sonic_runs_tools_and_returns_results():
    sent = []

    async def lookup(args):
        return f"two meetings ({args['question']})"

    s = Sonic(system_prompt="x", tools=[{"name": "lookup", "description": "d", "schema": {"type": "object"},
                                         "run": lookup}])

    async def fake_send(e):
        sent.append(json.loads(e)["event"])
    s._send = fake_send
    assert "toolConfiguration" in s._prompt_start()

    async def go():
        await s._handle({"toolUse": {"toolUseId": "t1", "toolName": "lookup",
                                     "content": json.dumps({"question": "what's on Thursday"})}})
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert [list(e)[0] for e in sent] == ["contentStart", "toolResult", "contentEnd"]
    assert sent[0]["contentStart"]["toolResultInputConfiguration"]["toolUseId"] == "t1"
    assert "two meetings" in json.loads(sent[1]["toolResult"]["content"])["result"]
    assert s.tool_calls[0][0] == "lookup"
