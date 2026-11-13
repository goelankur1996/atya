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
    assert out[-1].startswith("Try Physio One") and "Sources:\n- https://a.example/physio" in out[-1]
    assert out[-1].endswith("Saved as DOC-1.")
    t = owner_tenant(store)
    with store.tx(t["id"]) as tx:
        doc = tx._one("SELECT * FROM records WHERE type = 'document'")
        inbox = tx._one("SELECT * FROM inbox WHERE raw_content LIKE 'Research:%%'")
    assert doc["title"] == "Research: good physios in Noida" and doc["fields"]["Kind"] == "report"
    assert "https://b.example/flexi" in doc["body"]
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
