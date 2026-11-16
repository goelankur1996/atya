"""Calls: the queue (morning, reminder, now), retries per call, declines, consent,
falling back to messages, the opening prompt, weather, tools, and saving what was said.
(The call itself needs real Telegram accounts; here it's a stub.)"""
import asyncio
import datetime as dt
import json

import pytest

from atya import processor, receiver, reminders, wakecall, weather
from atya.config import Settings
from atya.schema import load_schema
from atya.sonic import Sonic
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram, owner_tenant, run_all

S = load_schema("default")
FRIEND, FRIEND2 = 777_000_501, 777_000_502


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
        tx.conn.execute("DELETE FROM calls")
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
        tx.conn.execute("DELETE FROM calls")


def friend(store, uid, name="Meera", username="meera_k", consent=False):
    with store.tx(system=True) as tx:
        t = tx.create_tenant(telegram_user_id=uid, telegram_chat_id=uid, display_name=name, timezone="Asia/Kolkata")
        tx.set_tenant_status(t["id"], "active", actor="test")
        tx.conn.execute("UPDATE tenants SET telegram_username = %s WHERE id = %s", (username, t["id"]))
        if consent:
            tx.set_wakecall_consent(t["id"])
        return tx.tenant(t["id"])


def ist(h, m=0, day=16):
    return dt.datetime(2026, 11, day, h, m, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))) \
        .astimezone(dt.timezone.utc)


class Phone:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, tenant, call):
        self.calls.append((call["kind"], call.get("about")))
        return self.results.pop(0)


def outbox(store, tid):
    with store.tx(tid) as tx:
        return [r["body"] for r in tx._all("SELECT body FROM outbox WHERE tenant_id = %s ORDER BY created_at",
                                           (tid,))]


def tenant(store, tid):
    with store.tx(system=True) as tx:
        return tx.tenant(tid)


def test_morning_call_retries_then_sends_the_brief(store, settings, owner):
    with store.tx(owner["id"]) as tx:
        tx.set_wakecall(owner["id"], dt.time(6, 0))
    phone = Phone("missed", "missed", "missed")
    assert wakecall.tick(store, settings, phone, now=ist(5, 59)) is None
    assert wakecall.tick(store, settings, phone, now=ist(6, 0)) == "morning:missed:retry"
    assert wakecall.tick(store, settings, phone, now=ist(6, 5)) is None             # waits 10 minutes
    assert wakecall.tick(store, settings, phone, now=ist(6, 11)) == "morning:missed:retry"
    assert wakecall.tick(store, settings, phone, now=ist(6, 22)) == "morning:missed:missed"
    assert wakecall.tick(store, settings, phone, now=ist(6, 40)) is None and len(phone.calls) == 3
    assert outbox(store, owner["id"])[-1].startswith(wakecall.MISSED_MORNING)
    assert wakecall.tick(store, settings, Phone("answered"), now=ist(6, 0, day=17)) == "morning:answered:answered"


def test_declining_one_call_doesnt_cancel_the_next(store, settings, owner):
    with store.tx(owner["id"]) as tx:
        tx.set_wakecall(owner["id"], dt.time(9, 0))
        tx.create_record(owner["id"], S, "task", title="Leave for the airport", enforce_verbatim=False, actor="t",
                         fields={"Remind At": "2026-11-16T14:00", "Remind By": "call", "Status": "next"})
    phone = Phone("declined", "answered")
    assert wakecall.tick(store, settings, phone, now=ist(9, 0)) == "morning:declined:declined"
    assert outbox(store, owner["id"])[-1].startswith(wakecall.DECLINED_MORNING)
    assert wakecall.tick(store, settings, phone, now=ist(9, 15)) is None              # no retry after a decline
    assert reminders.send_due_reminders(store, ist(14, 0), calls_on=True) >= 1
    assert wakecall.tick(store, settings, phone, now=ist(14, 0)) == "reminder:answered:answered"
    assert phone.calls == [("morning", None), ("reminder", "Leave for the airport")]


def test_reminder_call_falls_back_to_a_message(store, settings, owner):
    with store.tx(owner["id"]) as tx:
        tx.create_record(owner["id"], S, "task", title="Pay rent", enforce_verbatim=False, actor="t",
                         fields={"Remind At": "2026-11-16T10:00", "Remind By": "call", "Status": "next"})
    reminders.send_due_reminders(store, ist(10, 0), calls_on=True)
    assert outbox(store, owner["id"]) == []                                           # a call, not a message
    assert wakecall.tick(store, settings, Phone("declined"), now=ist(10, 0)) == "reminder:declined:declined"
    assert outbox(store, owner["id"])[-1].startswith("⏰ Pay rent") and "declined" in outbox(store, owner["id"])[-1]
    with store.tx(owner["id"]) as tx:                                                  # calls not set up: a message
        tx.create_record(owner["id"], S, "task", title="Call mom", enforce_verbatim=False, actor="t",
                         fields={"Remind At": "2026-11-16T11:00", "Remind By": "call", "Status": "next"})
    reminders.send_due_reminders(store, ist(11, 0), calls_on=False)
    assert outbox(store, owner["id"])[-1].startswith("⏰ Call mom")


def test_call_only_task_closes_after_the_call(store, settings, owner):
    with store.tx(owner["id"]) as tx:                  # what filing makes of "call me at 4" (no other purpose)
        tx.create_record(owner["id"], S, "task", title="Call from ATYA", enforce_verbatim=False, actor="t",
                         fields={"Status": "next", "Remind At": "2026-11-16T16:00", "Remind By": "call"})
    with store.tx(owner["id"]) as tx:
        (t,) = tx._all("SELECT * FROM records WHERE tenant_id = %s AND type = 'task'", (owner["id"],))
    assert t["title"] == "Call from ATYA" and t["fields"]["Remind By"] == "call"
    reminders.send_due_reminders(store, ist(16, 0), calls_on=True)
    assert wakecall.tick(store, settings, Phone("answered"), now=ist(16, 0)) == "reminder:answered:answered"
    with store.tx(owner["id"]) as tx:
        assert tx.record(owner["id"], t["id"])["fields"]["Status"] == "done"


def test_callme_and_wakecall_commands(store, settings, owner):
    tg = FakeTelegram()
    tg._next = 5000                       # after the fixture's /start
    tg.push("/wakecall 6:30")
    tg.push("/callme")
    run_all(store, tg, FakeLLM(), settings)
    assert owner_tenant(store)["wakecall_time"] == dt.time(6, 30)
    with store.tx(owner["id"]) as tx:
        assert "now" in [c["kind"] for c in tx.upcoming_calls(owner["id"])]
    assert wakecall.tick(store, settings, Phone("missed"),
                         now=dt.datetime.now(dt.timezone.utc)).startswith("now:missed:missed")
    assert "couldn't reach you" in outbox(store, owner["id"])[-1]                    # no retries for /callme
    tg.push("/wakecall off")
    run_all(store, tg, FakeLLM(), settings)
    assert owner_tenant(store)["wakecall_time"] is None


def test_others_opt_in_and_unconsented_calls_become_messages(store, settings, owner):
    from atya import commands
    meera = friend(store, FRIEND)
    with store.tx(system=True) as tx:
        tx.set_state(wakecall.CALLER_KEY, {"username": "atya_caller", "name": "ATYA"})

    def say(t, text):
        tg = FakeTelegram()
        ctx = commands.Ctx(store=store, tg=tg, settings=settings, tenant=t, channel="telegram",
                           user_id=t["telegram_user_id"], chat_id=t["telegram_chat_id"], name=t["display_name"])
        return commands.handle(ctx, text), [x for _, x, _ in tg.sent]

    out, r = say(meera, "/callme")
    assert out == "wakecall:intro" and "@atya_caller" in r[0] and "/wakecall agree" in r[0]
    with store.tx(meera["id"]) as tx:                     # a call asked for in chat before agreeing
        tx.create_record(meera["id"], S, "task", title="Stretch", enforce_verbatim=False, actor="t",
                         fields={"Remind At": "2026-11-16T12:00", "Remind By": "call", "Status": "next"})
    reminders.send_due_reminders(store, ist(12, 0), calls_on=True)
    phone = Phone()
    assert wakecall.tick(store, settings, phone, now=ist(12, 0)) == "reminder:no-consent:cancelled"
    assert phone.calls == [] and "/wakecall" in outbox(store, meera["id"])[-1]
    assert say(meera, "/wakecall agree")[0] == "wakecall:agreed"
    assert say(tenant(store, meera["id"]), "/callme")[0] == "wakecall:now"
    assert wakecall.tick(store, settings, Phone("unreachable"),
                         now=dt.datetime.now(dt.timezone.utc)) == "now:unreachable:unreachable"
    assert "@atya_caller" in outbox(store, meera["id"])[-1]


def test_daily_call_minutes(store, settings, owner):
    meera = friend(store, FRIEND, consent=True)
    assert wakecall.seconds_left(store, settings, meera) == wakecall.MAX_CALL_SECONDS
    wakecall.record_call(store, meera, 60 * 60 - 30)
    assert wakecall.seconds_left(store, settings, meera) == 30
    wakecall.record_call(store, owner, 10_000)
    assert wakecall.seconds_left(store, settings, owner) == wakecall.MAX_CALL_SECONDS     # owner exempt


def test_opening_prompt():
    t = {"display_name": "Ankur Goel"}
    local = dt.datetime(2026, 11, 16, 6, 0)
    p = wakecall.build_prompt(t, {"kind": "morning"}, local, "BRIEF", "matthew", None)
    assert "ask which city they're in" in p and "BRIEF" in p and "2026-11-16T06:00" in p
    p = wakecall.build_prompt({**t, "city": "Noida"}, {"kind": "morning"}, local, "BRIEF", "matthew",
                              "24°C and clear now; high 28, low 14.")
    assert "24°C and clear" in p and "what would they like to focus on" in p and "stop and wait" in p.lower()
    p = wakecall.build_prompt(t, {"kind": "reminder", "about": "leave for the airport"}, local, "B", "matthew", None)
    assert 'asked to be reminded: "leave for the airport"' in p
    # Everything about their records goes to ATYA (the same path as a text); a varied filler before each.
    assert "send it with atya" in p and "Never say you can't make a change" in p
    assert len(wakecall.FILLERS) >= 40 and len(set(wakecall.FILLERS)) == len(wakecall.FILLERS)
    assert all(f'"{f}"' in p for f in wakecall.FILLERS) and "{fillers}" not in p


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, params=None, timeout=None):
        body = self.routes[url]

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return body
        return R()


def test_weather_and_save_city(store, owner):
    http = FakeHTTP({
        weather.GEOCODE: {"results": [{"name": "Noida", "admin1": "Uttar Pradesh", "country": "India",
                                       "latitude": 28.58, "longitude": 77.33}]},
        weather.FORECAST: {"current": {"temperature_2m": 23.6, "weather_code": 1},
                           "daily": {"temperature_2m_max": [28.2], "temperature_2m_min": [14.4],
                                     "precipitation_probability_max": [60]}}})
    assert weather.today_line(28.58, 77.33, http=http) == \
        "24°C and mostly clear now; high 28, low 14; 60% chance of rain."
    out = wakecall.save_city(store, dict(owner), "noida", http=http)
    assert out.startswith("Saved Noida, Uttar Pradesh, India. Weather there today: 24°C")
    t = owner_tenant(store)
    assert t["city"] == "Noida, Uttar Pradesh, India" and round(t["city_lat"], 2) == 28.58
    assert weather.find_city("", http=http) is None


def call_with_processor(store, settings, llm, tenant_, said):
    """The call's atya tool and the processor at work together: returns what the tool would say back."""
    import threading
    requests, out = [], {}
    t = threading.Thread(target=lambda: out.setdefault("reply", wakecall.ask_atya(
        store, tenant_, said, requests, wait=5, poll=0.05)))
    t.start()
    while t.is_alive():
        processor.process_once(store, llm, settings)
    return out["reply"], requests


def test_call_requests_go_through_the_same_processor(store, settings, owner):
    llm = FakeLLM()
    llm.plans["Call Rahul about the audit tomorrow"] = {
        "writes": [{"type": "task", "title": "Call Rahul about the audit", "fields": {}}]}
    reply, requests = call_with_processor(store, settings, llm, owner, "Call Rahul about the audit tomorrow")
    assert reply.startswith("Saved:") and "Call Rahul about the audit" in reply
    assert outbox(store, owner["id"]) == [reply]          # spoken on the call (marked sent), not sent to the chat
    with store.tx(owner["id"]) as tx:
        assert tx._one("SELECT status FROM outbox WHERE inbox_id = %s", (requests[0],))["status"] == "sent"
    sonic = type("S", (), {"transcript": [("ASSISTANT", "Morning!"), ("USER", "call rahul about the audit")],
                           "tool_calls": [("atya", {}, reply)]})()
    wakecall.finish_talk(store, owner, {"kind": "now"}, sonic, 42, dt.datetime.now(dt.timezone.utc), requests)
    assert outbox(store, owner["id"])[-1].startswith("From our call:\n- ") and "Rahul" in outbox(store, owner["id"])[-1]
    with store.tx(owner["id"]) as tx:                    # the transcript is kept, not filed again
        (tr,) = tx._all("SELECT * FROM inbox WHERE tenant_id = %s AND sender = 'call'", (owner["id"],))
    assert tr["status"] == "processed" and "Ankur: call rahul about the audit" in tr["raw_content"]


def test_slow_call_request_answers_in_the_chat(store, settings, owner):
    from atya.store import CALL_REQUEST_LATE
    requests = []
    assert wakecall.ask_atya(store, owner, "research AQI in Gurgaon", requests, wait=0.2, poll=0.05) == \
        wakecall.LATE_REPLY
    with store.tx(owner["id"]) as tx:
        assert tx._one("SELECT sender FROM inbox WHERE id = %s", (requests[0],))["sender"] == CALL_REQUEST_LATE
        tx.add_outbox(owner["id"], chat_id=OWNER, body="AQI is 180", inbox_id=requests[0])
        assert tx._one("SELECT channel FROM outbox WHERE inbox_id = %s", (requests[0],))["channel"] != "call"


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


def test_call_runs_in_its_own_process(monkeypatch):
    import sys
    script = "import sys; print('hello from the call'); print('RESULT: missed')"
    real = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kw):
        return await real(sys.executable, "-c", script, **kw)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    assert asyncio.run(wakecall.call_in_own_process({"telegram_user_id": 1, "id": "abcd"}, {"id": "c1"})) == "missed"


def test_setup_by_call(store, settings, owner):
    from atya import commands, interview
    meera = friend(store, FRIEND)
    tg = FakeTelegram()
    ctx = commands.Ctx(store=store, tg=tg, settings=settings, tenant=meera, channel="telegram",
                       user_id=FRIEND, chat_id=FRIEND, name="Meera")
    assert commands.offer_setup(ctx, meera, head="You're in.") == "setup:offered"
    text = tg.sent[-1][1]
    assert "quick call" in text and [b[1] for b in tg.last_buttons[0]] == ["setup:call", "setup:text"]
    assert commands.setup_choice(store, settings, meera, FRIEND, "call", channel="telegram") == "setup:call"
    assert tenant(store, meera["id"])["wakecall_consent_at"] is not None              # the tap is the consent
    phone = Phone("answered")
    assert wakecall.tick(store, settings, phone, now=dt.datetime.now(dt.timezone.utc)) == "setup:answered:answered"
    # What she says goes to ATYA during the call; afterwards the usual summary and the 9 am brief.
    llm = FakeLLM()
    llm.plans["Dentist on Friday at 5"] = {"writes": [{"type": "task", "title": "Dentist", "fields": {}}]}
    _, requests = call_with_processor(store, settings, llm, tenant(store, meera["id"]), "Dentist on Friday at 5")
    sonic = type("S", (), {"transcript": [("ASSISTANT", "What's on this week?"), ("USER", "dentist on friday at 5")],
                           "tool_calls": []})()
    wakecall.finish_talk(store, tenant(store, meera["id"]), {"kind": "setup"}, sonic, 300,
                         dt.datetime.now(dt.timezone.utc), requests)
    assert outbox(store, meera["id"])[-1].startswith("That's it, thanks. I've got: 1 task.")
    assert tenant(store, meera["id"])["brief_time"] == dt.time(9, 0)


def test_setup_call_not_answered_falls_back_to_messages(store, settings, owner):
    from atya import commands, interview
    meera = friend(store, FRIEND2, "Ravi", None)
    commands.setup_choice(store, settings, meera, FRIEND2, "call", channel="telegram")
    assert wakecall.tick(store, settings, Phone("declined"),
                         now=dt.datetime.now(dt.timezone.utc)) == "setup:declined:declined"
    assert interview.TEXT_INSTEAD in outbox(store, meera["id"])[-1]
    with store.tx(meera["id"]) as tx:
        assert interview.current(tx, meera["id"]) is not None                          # message interview started
    assert commands.setup_choice(store, settings, meera, FRIEND2, "text", channel="telegram") == "setup:text"


def test_set_morning_call_tool(store, owner):
    assert wakecall.set_morning_call(store, owner, "6:45", now=ist(9)) == \
        "Done: a call every morning at 6:45 am (Asia/Kolkata), starting tomorrow."
    assert owner_tenant(store)["wakecall_time"] == dt.time(6, 45)
    assert wakecall.set_morning_call(store, owner, "off") == "Morning call turned off."


def test_call_starts_with_their_records(store, settings, owner):
    from atya import briefs
    with store.tx(owner["id"]) as tx:
        tx.create_record(owner["id"], S, "task", title="Storytelling with Data workshop", enforce_verbatim=False,
                         actor="t", fields={"Status": "next", "Due Date": "2026-11-19",
                                            "Notes": "Lemon Tree Hotel, Sector 60, 9:30 to 5:30"})
        tx.create_record(owner["id"], S, "task", title="Renew passport", enforce_verbatim=False, actor="t",
                         fields={"Status": "inbox"})
        tx.create_record(owner["id"], S, "task", title="Old thing", enforce_verbatim=False, actor="t",
                         fields={"Status": "done"})
        tx.create_record(owner["id"], S, "person", title="Rahul", enforce_verbatim=False, actor="t",
                         fields={"Birthday": "11-25", "Relationship": "friend"})
        text = briefs.call_records(tx, S, owner["id"], dt.date(2026, 11, 17))
    assert "Storytelling with Data workshop; due Thu 19 Nov; Lemon Tree Hotel" in text
    assert "- Renew passport" in text and "Old thing" not in text
    assert "Rahul: birthday 11-25" in text and "- Rahul (relationship friend)" in text
    p = wakecall.build_prompt({"display_name": "Ankur"}, {"kind": "now"}, dt.datetime(2026, 11, 17, 9), "B",
                              "matthew", None, records=text)
    assert p.endswith(text) and "answer\n  straight away without atya" in p


def test_call_questions_plan_with_the_fast_model(store, settings, owner):
    llm = FakeLLM()
    llm.intent["What's on Friday 2026-11-20?"] = "query"
    llm.query_plans["What's on Friday 2026-11-20?"] = {"types": ["task"], "search": "friday"}
    reply, _ = call_with_processor(store, settings, llm, owner, "What's on Friday 2026-11-20?")
    assert reply and llm.last_fast is True
