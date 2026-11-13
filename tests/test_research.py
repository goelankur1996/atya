"""Web research: explicit and classified requests, reply with sources, saved as a report, daily cap."""

from __future__ import annotations

from dataclasses import replace

import pytest

from atya import llm as L, processor, receiver, research, sender
from atya.config import Settings
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram, owner_tenant

FRIEND = 880004


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2,
                    daily_research=1)


@pytest.fixture
def clean(store):
    with store.tx(system=True) as tx:
        for t in tx._all("SELECT id FROM tenants WHERE telegram_user_id = ANY(%s)", ([OWNER, FRIEND],)):
            tx.delete_tenant(t["id"])
        tx.conn.execute("DELETE FROM service_state")
        tx.conn.execute("UPDATE inbox SET status = 'processed' WHERE status IN ('pending', 'processing')")
    yield


class FakeResearcher:
    def __init__(self):
        self.calls = []
        self.usage = {"nova": {"calls": 0, "in": 0, "out": 0, "secs": 0}}

    def research(self, topic, *, timezone, today):
        self.calls.append((topic, timezone, today))
        self.usage["nova"]["in"] += 500
        return {"text": "Try Physio One in Sector 62 and FlexiCare in Sector 18.",
                "sources": ["https://a.example/physio", "https://b.example/flexi"]}


def run(store, tg, llm, settings, rs):
    receiver.poll_once(store, tg, settings, timeout=0)
    while processor.process_once(store, llm, settings, None, None, rs):
        pass
    sender.send_once(store, tg)


def mine(tg, chat=555):
    return [m[1] for m in tg.sent if m[0] == chat]


def test_explicit_research_replies_and_saves_a_report(store, settings, clean):
    tg, llm, rs = FakeTelegram(), FakeLLM(), FakeResearcher()
    tg.push("/start")
    tg.push("Research: good physios in Noida")
    run(store, tg, llm, settings, rs)
    assert rs.calls[0][0] == "good physios in Noida" and rs.calls[0][1] == "Asia/Kolkata"
    out = mine(tg)
    assert out[-2] == processor.RESEARCH_ACK
    assert out[-1].startswith("Written: Try Physio One") and "Sources:\n- https://a.example/physio" in out[-1]
    assert out[-1].endswith("Saved as DOC-1 with the full write-up.")
    t = owner_tenant(store)
    with store.tx(t["id"]) as tx:
        doc = tx._one("SELECT * FROM records WHERE type = 'document'")
        inbox = tx._one("SELECT * FROM inbox WHERE raw_content LIKE 'Research:%%'")
    assert doc["title"] == "Research: good physios in Noida" and doc["fields"]["Kind"] == "report"
    assert "https://b.example/flexi" in doc["body"] and doc["body"].startswith("Request: good physios in Noida")
    assert inbox["intent"] == "research" and inbox["status"] == "responded"


def test_classified_research_and_daily_cap(store, settings, clean):
    tg, llm, rs = FakeTelegram(), FakeLLM(), FakeResearcher()
    with store.tx(system=True) as tx:
        tx.create_tenant(telegram_user_id=FRIEND, telegram_chat_id=FRIEND, timezone="Europe/London")
    q1, q2 = "find me 3 good physios in Noida", "compare the Kindle and the Kobo"
    llm.intent[q1] = llm.intent[q2] = "research"
    tg.push(q1, user_id=FRIEND, chat_id=FRIEND)
    tg.push(q2, user_id=FRIEND, chat_id=FRIEND)
    run(store, tg, llm, settings, rs)
    assert len(rs.calls) == 1 and rs.calls[0][1] == "Europe/London"
    assert "today's 1 research requests" in mine(tg, FRIEND)[-1]


def test_research_off_says_so(store, settings, clean):
    tg, llm = FakeTelegram(), FakeLLM()
    tg.push("/start")
    tg.push("research flights to Goa")
    run(store, tg, llm, settings, None)
    assert "research isn't switched on" in mine(tg)[-1]


def test_explicit_prefix():
    assert research.explicit("Research: visa rules for Japan") == "visa rules for Japan"
    assert research.explicit("research the best budget hotels in Goa") == "the best budget hotels in Goa"
    assert research.explicit("researchers say") is None
    assert research.explicit("look into Voyage pricing") is None
    assert research.explicit("research") is None


def test_nova_researcher_parses_text_and_citations():
    class C:
        def converse(self, **kw):
            assert kw["toolConfig"] == {"tools": [{"systemTool": {"name": "nova_grounding"}}]}
            assert "timezone Asia/Kolkata" in kw["system"][0]["text"]
            return {"output": {"message": {"content": [
                {"reasoningContent": {"x": 1}},
                {"text": "Answer part one. "},
                {"citationsContent": {"citations": [{"location": {"web": {"url": "https://x.example", "domain": "x"}}},
                                                    {"location": {"web": {"url": "https://x.example"}}}]}},
                {"text": "Part two."}]}}, "usage": {"inputTokens": 900, "outputTokens": 200}}
    r = research.NovaResearcher(client=C())
    out = r.research("q", timezone="Asia/Kolkata", today="Friday 13 November 2026")
    assert out == {"text": "Answer part one. Part two.", "sources": ["https://x.example"]}
    assert r.usage[research.MODEL]["in"] == 900


def test_classifier_accepts_research():
    class C:
        def converse(self, **kw):
            return {"output": {"message": {"content": [{"text": '{"intent": "research"}'}]}}, "usage": {}}
    assert L.BedrockLLM(fast_model="f", main_model="m", client=C()).classify("find me a physio")["intent"] == "research"


def test_asks_first_when_details_are_missing_then_searches_from_several_angles(store, settings, clean):
    tg, llm, rs = FakeTelegram(), FakeLLM(), FakeResearcher()
    llm.research_plans = [{"ask": ["What's your budget per night?", "Which dates?"],
                           "queries": ["budget hotels Goa", "Goa hotels reviews December", "Goa hostels prices"],
                           "goal": "3 hotels with prices"}] * 2
    tg.push("/start")
    tg.push("research hotels in Goa")
    run(store, tg, llm, settings, rs)
    out = mine(tg)
    assert out[-1].startswith("Before I search") and "1. What's your budget per night?" in out[-1]
    assert rs.calls == []                                     # nothing searched yet, nothing counted

    tg.push("Under 4000 a night, 20 to 23 Dec")
    run(store, tg, llm, settings, rs)
    assert sorted(c[0] for c in rs.calls) == ["Goa hostels prices", "Goa hotels reviews December", "budget hotels Goa"]
    last = llm.plan_calls[-1]
    assert last["answers"] == "Under 4000 a night, 20 to 23 Dec" and last["allow_questions"] is False
    assert mine(tg)[-1].startswith("Written: ")
    t = owner_tenant(store)
    with store.tx(t["id"]) as tx:
        doc = tx._one("SELECT * FROM records WHERE type = 'document'")
        assert tx.get_state(research.pending_key(t["id"])) is None
    assert "Under 4000 a night" in doc["body"] and doc["body"].count("Try Physio One") == 3


def test_just_search_skips_the_questions(store, settings, clean):
    tg, llm, rs = FakeTelegram(), FakeLLM(), FakeResearcher()
    llm.research_plans = [{"ask": ["Which city?"], "queries": ["physios"], "goal": ""}] * 2
    tg.push("/start")
    tg.push("research physios")
    run(store, tg, llm, settings, rs)
    tg.push("just search")
    run(store, tg, llm, settings, rs)
    assert len(rs.calls) == 1 and llm.plan_calls[-1]["answers"] == ""


def test_all_searches_failing_is_retried_then_reported(store, settings, clean):
    class Down(FakeResearcher):
        def research(self, topic, **kw):
            raise RuntimeError("throttled")
    tg, llm = FakeTelegram(), FakeLLM()
    tg.push("/start")
    tg.push("research flights to Goa")
    for _ in range(3):
        run(store, tg, llm, settings, Down())
        with store.tx(system=True) as tx:
            tx.conn.execute("UPDATE inbox SET claimed_at = now() - interval '1 hour'")
    assert mine(tg)[-1] == processor.FAILED_REPLY
    assert mine(tg).count(processor.RESEARCH_ACK) == 1          # the "looking into it" isn't repeated


def test_plan_and_write_prompts():
    class C:
        def __init__(self, reply):
            self.reply, self.kw = reply, None

        def converse(self, **kw):
            self.kw = kw
            return {"output": {"message": {"content": [{"text": self.reply}]}}, "usage": {}}
    c = C('{"ask": ["a?", "b?", "c?", "d?"], "queries": ["q1", "q2", "q3", "q4", "q5"], "goal": "g"}')
    b = L.BedrockLLM(fast_model="f", main_model="m", client=c)
    p = b.research_plan(request="hotels", answers=None, allow_questions=True, timezone="Asia/Kolkata", today="x")
    assert p == {"ask": ["a?", "b?", "c?"], "queries": ["q1", "q2", "q3", "q4"], "goal": "g"}
    p = b.research_plan(request="hotels", answers="cheap", allow_questions=False, timezone="Asia/Kolkata", today="x")
    assert p["ask"] == [] and "Do not ask anything" in c.kw["messages"][0]["content"][0]["text"]
    c.reply = '{"reply": "short", "report": "long"}'
    w = b.research_write(request="r", goal="g", findings=[{"query": "q", "text": "t", "sources": ["https://s"]}])
    assert w == {"reply": "short", "report": "long"}
    assert "Sources: https://s" in c.kw["messages"][0]["content"][0]["text"]
