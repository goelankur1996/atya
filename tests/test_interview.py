"""The setup interview: starts after sign-up, files each answer, follows up, wraps up, turns on the brief."""

from __future__ import annotations

import datetime as dt

import pytest

from atya import commands, interview, llm as L, processor, receiver, sender
from atya.config import Settings
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram

FRIEND = 880003


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


def test_interview_files_answers_follows_up_and_wraps_up(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = join(store, settings, tg)
    first = to_friend(tg)[-1]
    assert first.startswith("Timezone set to Asia/Kolkata") and interview.FIRST_QUESTION in first

    a1 = "Running my export business with Meera, and training for a half marathon"
    llm.plans[a1] = {"writes": [{"type": "project", "title": "Export business", "fields": {}},
                                {"type": "person", "title": "Meera", "fields": {}}]}
    a2 = "Need to send Meera the pricing sheet by Friday"
    llm.plans[a2] = {"writes": [{"type": "task", "title": "Send Meera the pricing sheet", "fields": {"Status": "next"}}]}
    llm.interview_script = [
        {"done": False, "question": "What's open with Meera right now?", "suggest": ""},
        {"done": False, "question": "When is the half marathon?", "suggest": ""},
        {"done": True, "question": "", "suggest": "What do I owe Meera?"},
    ]
    say(store, tg, llm, settings, a1)
    assert to_friend(tg)[-1] in ("Saved 1 project, 1 person.\n\nWhat's open with Meera right now?",
                                 "Saved 1 person, 1 project.\n\nWhat's open with Meera right now?")
    say(store, tg, llm, settings, a2)
    assert to_friend(tg)[-1].endswith("When is the half marathon?")
    say(store, tg, llm, settings, "skip")                  # skipped: nothing filed, model decides it's done
    end = to_friend(tg)[-1]
    assert end.startswith("That's it, thanks. I've got:") and "1 task" in end and "1 person" in end
    assert "What do I owe Meera?" in end
    with store.tx(t["id"]) as tx:
        assert tx.get_state(interview.key(t["id"])) is None
        tenant = tx.tenant(t["id"])
        inbox = tx._all("SELECT status, intent FROM inbox WHERE tenant_id = %s AND intent IS DISTINCT FROM 'command'",
                        (t["id"],))
    assert tenant["brief_time"] == dt.time(9, 0)           # the morning brief is now on
    assert {(r["status"], r["intent"]) for r in inbox} == {("processed", "log")}

    # After the interview, messages go back to normal handling.
    llm.intent["hi"] = "chat"
    say(store, tg, llm, settings, "hi")
    assert to_friend(tg)[-1] == "Hi there."


def test_stop_ends_early_and_setup_restarts(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = join(store, settings, tg)
    say(store, tg, llm, settings, "Stop.")
    assert "another time (send /setup)" in to_friend(tg)[-1]
    with store.tx(t["id"]) as tx:
        assert tx.tenant(t["id"])["brief_time"] is None      # nothing answered: brief left alone
    ctx = commands.Ctx(store=store, tg=tg, settings=settings, user_id=FRIEND, chat_id=FRIEND, name=None,
                       tenant=t)
    assert commands.handle(ctx, "/setup") == "setup:started"
    assert interview.FIRST_QUESTION in to_friend(tg)[-1]


def test_max_turns_and_abandoned_interview(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    t = join(store, settings, tg)
    llm.interview_script = [{"done": False, "question": f"Q{i}?", "suggest": ""} for i in range(20)]
    for i in range(interview.MAX_TURNS):
        say(store, tg, llm, settings, "skip")
    assert to_friend(tg)[-1].startswith("No problem")        # all skipped, stopped at the limit
    assert llm.interview_calls == interview.MAX_TURNS - 1

    with store.tx(t["id"]) as tx:
        interview.begin(tx, t["id"])
        st = tx.get_state(interview.key(t["id"]))
        st["asked"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).isoformat()
        tx.set_state(interview.key(t["id"]), st)
    llm.intent["hello"] = "chat"
    say(store, tg, llm, settings, "hello")
    assert to_friend(tg)[-1] == "Hi there."                   # handled normally; interview ended quietly


def test_bad_model_reply_wraps_up(store, settings, clean):
    tg = FakeTelegram()

    class Broken(FakeLLM):
        def interview_next(self, **kw):
            raise L.LLMError("non-JSON response")
    llm = Broken()
    join(store, settings, tg)
    a = "Building a robot arm"
    llm.plans[a] = {"writes": [{"type": "project", "title": "Robot arm", "fields": {}}]}
    say(store, tg, llm, settings, a)
    assert to_friend(tg)[-1].startswith("That's it, thanks")


def test_interview_prompt_and_parsing():
    class C:
        def converse(self, **kw):
            assert "number 3 of at most 10" in kw["system"][0]["text"]
            return {"output": {"message": {"content": [{"text": '{"done": false, "question": "Who else?"}'}]}},
                    "usage": {}}
    b = L.BedrockLLM(fast_model="f", main_model="m", client=C())
    out = b.interview_next(transcript=[{"q": "A?", "a": "x"}, {"q": "B?", "a": None}], n=3, max_turns=10)
    assert out == {"done": False, "question": "Who else?", "suggest": ""}
    assert interview.classify_reply(" Skip ") == "skip" and interview.classify_reply("that's all!") == "stop"
    assert interview.classify_reply("I want to stop smoking") == "answer"
