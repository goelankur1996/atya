"""The setup interview: starts after sign-up, files each answer, follows up, covers the essentials, wraps up."""

from __future__ import annotations

import datetime as dt

import pytest

from atya import commands, interview, llm as L, processor, receiver, sender
from atya.config import Settings
from atya.schema import load_schema
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram

FRIEND = 880003
ALL = list(interview.AREAS)


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
        tx.conn.execute("UPDATE inbox SET status = 'processed' WHERE status IN ('pending', 'processing')")
    yield


def to_friend(tg):
    return [m[1] for m in tg.sent if m[0] == FRIEND]


def join(store, settings, tg):
    """Owner invites, friend signs up and picks a timezone."""
    tg.push("/start")
    tg.push("/invite")
    receiver.poll_once(store, tg, settings, timeout=0)
    code = tg.sent[-1][1].split("Invite code (one use): ")[1].split()[0]
    tg.push(f"/start {code}", user_id=FRIEND, chat_id=FRIEND)
    tg.push_tap("tz:Asia/Kolkata", user_id=FRIEND, chat_id=FRIEND)
    receiver.poll_once(store, tg, settings, timeout=0)
    with store.tx(system=True) as tx:
        return tx.tenant_by_telegram(FRIEND)


def say(store, tg, llm, settings, text):
    tg.push(text, user_id=FRIEND, chat_id=FRIEND)
    receiver.poll_once(store, tg, settings, timeout=0)
    while processor.process_once(store, llm, settings):
        pass
    sender.send_once(store, tg)


def nxt(q, covered=(), done=False, suggest=""):
    return {"done": done, "question": q, "covered": list(covered), "suggest": suggest}


def test_interview_files_answers_follows_up_and_wraps_up(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = join(store, settings, tg)
    first = to_friend(tg)[-1]
    assert first.startswith("Timezone set to Asia/Kolkata") and interview.FIRST_QUESTION in first

    a1 = "Pricing call with Meera on Thursday, and the export deck is due Friday"
    llm.plans[a1] = {"writes": [{"type": "person", "title": "Meera", "fields": {}},
                                {"type": "task", "title": "Export deck", "fields": {"Status": "next"}}]}
    llm.interview_script = [nxt("What time is the call with Meera?", ["week"]),
                            nxt("Any birthdays coming up?", ALL, done=True, suggest="What do I owe Meera?")]
    say(store, tg, llm, settings, a1)
    reply = to_friend(tg)[-1]
    assert reply.startswith("Saved ") and "(2 so far)" in reply and reply.endswith("What time is the call with Meera?")
    assert llm.interview_seen["still_open"][0] == "urgent" and llm.interview_seen["target"] == interview.TARGET_RECORDS

    # The model says it's done, but only 2 questions in and 2 things saved: it must keep going.
    say(store, tg, llm, settings, "skip")
    assert to_friend(tg)[-1] == "Any birthdays coming up?"
    with store.tx(t["id"]) as tx:
        st = tx.get_state(interview.key(t["id"]))
        st["saved"] = interview.TARGET_RECORDS                  # pretend plenty was captured
        tx.set_state(interview.key(t["id"]), st)
    llm.interview_script = [nxt("", ALL, done=True, suggest="What do I owe Meera?")]
    say(store, tg, llm, settings, "skip")
    end = to_friend(tg)[-1]
    assert end.startswith("That's it, thanks. I've got:") and "1 task" in end and "1 person" in end
    assert "What do I owe Meera?" in end
    with store.tx(t["id"]) as tx:
        assert tx.get_state(interview.key(t["id"])) is None
        assert tx.tenant(t["id"])["brief_time"] == dt.time(9, 0)           # the morning brief is now on

    llm.intent["hi"] = "chat"
    say(store, tg, llm, settings, "hi")
    assert to_friend(tg)[-1] == "Hi there."                              # back to normal handling


def test_open_areas_get_asked_when_the_model_gives_up(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    join(store, settings, tg)
    llm.interview_script = [nxt("", ["week", "urgent"], done=True)]
    say(store, tg, llm, settings, "skip")
    assert to_friend(tg)[-1] == interview.FALLBACK["occasions"]


def test_model_error_falls_back_to_fixed_questions(store, settings, clean):
    tg = FakeTelegram()

    class Broken(FakeLLM):
        def interview_next(self, **kw):
            raise L.LLMError("non-JSON response")
    llm = Broken()
    join(store, settings, tg)
    say(store, tg, llm, settings, "skip")                    # 'week' skipped, so 'urgent' is next
    assert to_friend(tg)[-1] == interview.FALLBACK["urgent"]


def test_stop_ends_early_and_setup_restarts(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = join(store, settings, tg)
    say(store, tg, llm, settings, "Stop.")
    assert "another time (send /setup)" in to_friend(tg)[-1]
    with store.tx(t["id"]) as tx:
        assert tx.tenant(t["id"])["brief_time"] is None      # nothing answered: brief left alone
    ctx = commands.Ctx(store=store, tg=tg, settings=settings, user_id=FRIEND, chat_id=FRIEND, name=None, tenant=t)
    assert commands.handle(ctx, "/setup") == "setup:started"
    assert interview.FIRST_QUESTION in to_friend(tg)[-1]


def test_hard_stop_and_abandoned_interview(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = join(store, settings, tg)
    llm.interview_script = [nxt(f"Q{i}?") for i in range(40)]
    for _ in range(interview.MAX_TURNS):
        say(store, tg, llm, settings, "skip")
    assert to_friend(tg)[-1].startswith("No problem")         # stopped at MAX_TURNS
    assert llm.interview_calls == interview.MAX_TURNS - 1

    with store.tx(t["id"]) as tx:
        interview.begin(tx, t["id"])
        st = tx.get_state(interview.key(t["id"]))
        st["asked"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).isoformat()
        tx.set_state(interview.key(t["id"]), st)
    llm.intent["hello"] = "chat"
    say(store, tg, llm, settings, "hello")
    assert to_friend(tg)[-1] == "Hi there."                   # handled normally; interview ended quietly


def test_may_finish_rules():
    st = {"turns": [{}] * 5, "covered": ALL, "saved": interview.TARGET_RECORDS}
    assert interview.may_finish(st)
    assert not interview.may_finish({**st, "covered": ALL[:-1]})                  # an area still open
    assert not interview.may_finish({**st, "saved": 3})                           # too little, too early
    assert interview.may_finish({**st, "saved": 3, "turns": [{}] * interview.MIN_TURNS})
    assert interview.may_finish({"turns": [{}] * interview.MAX_TURNS, "covered": [], "saved": 0})


def test_interview_prompt_and_parsing():
    class C:
        def converse(self, **kw):
            sys_text = kw["system"][0]["text"]
            user = kw["messages"][0]["content"][0]["text"]
            assert "occasions: birthdays" in sys_text
            assert "Areas still open, in order: travel, goals" in user and "aim: at least 15" in user
            return {"output": {"message": {"content": [{"text":
                    '{"covered": ["week", "bogus"], "question": "Who else?", "done": false}'}]}}, "usage": {}}
    b = L.BedrockLLM(fast_model="f", main_model="m", client=C())
    out = b.interview_next(transcript=[{"q": "A?", "a": "x"}, {"q": "B?", "a": None}], areas=interview.AREAS,
                           still_open=["travel", "goals"], saved=4, target=15)
    assert out == {"done": False, "question": "Who else?", "covered": ["week"], "suggest": ""}
    assert interview.classify_reply(" Skip ") == "skip" and interview.classify_reply("that's all!") == "stop"
    assert interview.classify_reply("I want to stop smoking") == "answer"


def test_schema_has_birthdays():
    s = load_schema("default")
    assert "Birthday" in s.types["person"].fields
    assert "happy birthday" in s.rules_routing and "Trip:" in s.rules_routing
