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
        for u in (FRIEND, FRIEND2):
            f = tx.tenant_by_telegram(u)
            if f:
                tx.delete_tenant(f["id"])
    tg = FakeTelegram()
    tg.push("/start")
    run_all(store, tg, FakeLLM(), settings)
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        tx.set_timezone(t["id"], "Asia/Kolkata")
    yield owner_tenant(store)
    with store.tx(system=True) as tx:
        tx.conn.execute("DELETE FROM service_state WHERE key LIKE 'wakecall:%%'")


FRIEND, FRIEND2 = 777_000_501, 777_000_502


def friend(store, uid, name="Meera", username="meera_k"):
    with store.tx(system=True) as tx:
        t = tx.create_tenant(telegram_user_id=uid, telegram_chat_id=uid, display_name=name, timezone="Asia/Kolkata")
        tx.set_tenant_status(t["id"], "active", actor="test")
        tx.conn.execute("UPDATE tenants SET telegram_username = %s WHERE id = %s", (username, t["id"]))
        return tx.tenant(t["id"])


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
        tx.set_state(wakecall.now_key(owner["id"]), "x")
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
        assert tx.get_state(wakecall.now_key(owner["id"]))
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
    assert "From our call, saved:" in outbox(store, owner["id"])[-1]
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


def test_others_opt_in_within_the_beta_cap(store, settings, owner):
    from atya import commands
    from dataclasses import replace
    small = replace(settings, call_beta_users=1)
    meera, ravi = friend(store, FRIEND), friend(store, FRIEND2, "Ravi", None)
    with store.tx(system=True) as tx:
        tx.set_state(wakecall.CALLER_KEY, {"username": "atya_caller", "name": "ATYA"})

    def say(tenant, text):
        tg = FakeTelegram()
        ctx = commands.Ctx(store=store, tg=tg, settings=small, tenant=tenant, channel="telegram",
                           user_id=tenant["telegram_user_id"], chat_id=tenant["telegram_chat_id"],
                           name=tenant["display_name"])
        return commands.handle(ctx, text), [t for _, t, _ in tg.sent]

    out, r = say(meera, "/callme")
    assert out == "wakecall:intro" and "@atya_caller" in r[0] and "/wakecall agree" in r[0]
    assert say(meera, "/wakecall agree")[0] == "wakecall:agreed"
    assert say(ravi, "/wakecall agree")[0] == "wakecall:full"               # cap of 1 (the owner doesn't count)
    meera = tx_tenant(store, meera["id"])
    assert say(meera, "/wakecall 7:15")[0] == "wakecall:set"
    assert say(meera, "/callme")[0] == "wakecall:now"
    with store.tx(system=True) as tx:
        tx.set_state(wakecall.now_key(owner["id"]), None)
    phone = Phone("unreachable")
    assert wakecall.tick(store, small, phone, now=ist(14)) == "on-request:unreachable"
    assert "@atya_caller" in outbox(store, meera["id"])[-1]


def test_daily_call_minutes(store, settings, owner):
    meera = friend(store, FRIEND)
    assert wakecall.seconds_left(store, settings, meera) == wakecall.MAX_CALL_SECONDS
    wakecall.record_call(store, meera, settings.daily_call_minutes * 60 - 30)
    assert wakecall.seconds_left(store, settings, meera) == 30
    wakecall.record_call(store, owner, 10_000)
    assert wakecall.seconds_left(store, settings, owner) == wakecall.MAX_CALL_SECONDS     # owner exempt


def test_scheduled_call_for_a_friend(store, settings, owner):
    meera = friend(store, FRIEND)
    with store.tx(meera["id"]) as tx:
        tx.set_wakecall(meera["id"], dt.time(7, 0))
    phone = Phone("answered")
    assert wakecall.tick(store, settings, phone, now=ist(7, 1)) == "answered:1"
    assert wakecall.tick(store, settings, phone, now=ist(7, 2)) is None


def tx_tenant(store, tid):
    with store.tx(system=True) as tx:
        return tx.tenant(tid)


def test_call_runs_in_its_own_process(monkeypatch):
    import sys
    script = "import sys; print('hello from the call'); print('RESULT: missed')"
    real = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kw):
        return await real(sys.executable, "-c", script, **kw)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(wakecall.call_in_own_process({"telegram_user_id": 1, "id": "abcd"})) == "missed"
