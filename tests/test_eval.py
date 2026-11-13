"""Model evaluation: Bedrock transport, replay of past messages, report pages."""

from __future__ import annotations

import json

import pytest

from atya import eval as ev, llm as L, web
from atya.config import Settings
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram, clean, owner_tenant, run_all  # noqa: F401
from tests.test_web import sign_in


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2,
                    web_base_url="https://atya.test")


class FakeConverse:
    """Answers like a decent model, keyed on which prompt it gets."""

    def __init__(self, *, reject_system=False, garbage=False):
        self.calls = []
        self.reject_system = reject_system
        self.garbage = garbage

    def converse(self, *, modelId, messages, inferenceConfig, system=None):
        if system is not None and self.reject_system:
            raise RuntimeError("ValidationException: This model doesn't support system messages")
        sys_text = system[0]["text"] if system else messages[0]["content"][0]["text"]
        user = messages[0]["content"][-1]["text"]
        self.calls.append((modelId, sys_text[:40], messages[0]["content"]))
        if self.garbage:
            out = "I think this is a task."
        elif sys_text.startswith(L.CLASSIFY_SYSTEM[:40]):
            out = json.dumps({"intent": "query" if "?" in user else "log", "reply": ""})
        elif sys_text.startswith(L.FANOUT_SYSTEM[:40]):
            out = "<think>hmm {not json}</think>" + json.dumps(
                {"writes": [{"type": "task", "title": "Send Rahul the DROID audit"}], "updates": []})
        elif sys_text.startswith(L.PLAN_SYSTEM[:40]):
            out = json.dumps({"types": ["task"], "search": "Rahul"})
        elif sys_text.startswith(L.BROADEN_SYSTEM[:40]):
            out = json.dumps({"search": "Rahul audit"})
        elif sys_text.startswith(L.ANSWER_SYSTEM[:40]):
            out = json.dumps({"found": True, "answer": "Send Rahul the DROID audit (TSK-1)."})
        else:
            out = json.dumps({"title": "Doc", "kind": "other", "summary": "s"})
        return {"output": {"message": {"content": [{"reasoningContent": {"x": 1}}, {"text": out}]}},
                "usage": {"inputTokens": 100, "outputTokens": 20}}


def seed(store, settings):
    tg, llm = FakeTelegram(), FakeLLM()
    msg = "Met Rahul from Addverb. Need to send him the DROID audit by Friday."
    llm.plans[msg] = {"writes": [
        {"type": "task", "title": "Send Rahul the DROID audit", "fields": {"Status": "next"}},
        {"type": "person", "title": "Rahul", "fields": {"Company": "Addverb"}},
    ]}
    tg.push(msg)
    q = "What do I owe Rahul?"
    llm.intent[q] = "query"
    llm.query_plans[q] = {"types": ["task"], "search": "Rahul"}
    tg.push(q)
    run_all(store, tg, llm, settings)


def test_bedrock_transport_strips_thinking_and_counts_usage():
    fake = FakeConverse()
    b = L.BedrockLLM(fast_model="fast", main_model="main", client=fake)
    assert b.classify("buy milk") == {"intent": "log", "reply": ""}
    b.read_document(filename="x.pdf", caption="", pdf=b"%PDF-1.4")
    content = fake.calls[-1][2]
    assert content[0]["document"]["format"] == "pdf" and content[0]["document"]["source"]["bytes"] == b"%PDF-1.4"
    assert b.usage["fast"]["calls"] == 2 and b.usage["fast"]["in"] == 200


def test_bedrock_falls_back_when_system_prompt_rejected():
    fake = FakeConverse(reject_system=True)
    b = L.BedrockLLM(fast_model="m", main_model="m", client=fake)
    assert b.classify("is the deck done?")["intent"] == "query"
    assert "m" in b._no_system


def test_eval_replays_without_writing(store, settings, clean):  # noqa: F811
    seed(store, settings)
    t = owner_tenant(store)
    with store.tx(t["id"]) as tx:
        before = tx._one("SELECT (SELECT count(*) FROM records) AS r, (SELECT count(*) FROM outbox) AS o, "
                         "(SELECT count(*) FROM inbox) AS i")
    b = L.BedrockLLM(fast_model="nova", main_model="qwen", client=FakeConverse())
    report = ev.run_eval(store, b, t, limit=50, workers=2)
    s = report["summary"]
    assert s["messages"] == 2 and s["errors"] == 0
    assert s["intent_agree"] == 2
    assert s["records_original"] == 2 and s["records_matched"] == 1        # missed the person
    assert s["found_agree"] == 1
    log_item = next(i for i in report["items"] if i["original_intent"] == "log")
    assert [m["type"] for m in log_item["match"]["missed"]] == ["person"]
    q = next(i for i in report["items"] if i["original_intent"] == "query")
    assert q["answer"].startswith("Send Rahul") and q["original_answer"]
    with store.tx(t["id"]) as tx:
        after = tx._one("SELECT (SELECT count(*) FROM records) AS r, (SELECT count(*) FROM outbox) AS o, "
                        "(SELECT count(*) FROM inbox) AS i")
    assert after == before
    with store.tx(system=True) as tx:
        assert tx.get_state("eval:index")[0]["id"] == report["id"]


def test_eval_counts_bad_format_as_failures(store, settings, clean):  # noqa: F811
    seed(store, settings)
    b = L.BedrockLLM(fast_model="tiny", main_model="tiny", client=FakeConverse(garbage=True))
    report = ev.run_eval(store, b, owner_tenant(store), limit=50, workers=2, with_files=False)
    assert report["summary"]["errors"] == 2
    assert all("LLMError" in i["error"] for i in report["items"])


def test_report_pages_owner_only(store, settings, clean):  # noqa: F811
    seed(store, settings)
    t = owner_tenant(store)
    report = ev.run_eval(store, L.BedrockLLM(fast_model="nova", main_model="qwen", client=FakeConverse()),
                         t, limit=50, workers=1)
    app = web.create_app(store, settings)
    c, _ = sign_in(app, store, settings, t)
    page = c.get(f"/eval/{report['id']}").get_data(as_text=True)
    assert "Send Rahul the DROID audit" in page and "qwen" in page and "style=" not in page
    assert report["id"] in c.get("/eval").get_data(as_text=True)
    assert c.get("/eval/../db").status_code == 404
    assert c.get("/eval/20990101-000000").status_code == 404

    with store.tx(system=True) as tx:
        friend = tx.create_tenant(telegram_user_id=777001, telegram_chat_id=777001, timezone="UTC")
    try:
        c2, _ = sign_in(app, store, settings, friend)
        assert c2.get("/eval").status_code == 404
        assert c2.get(f"/eval/{report['id']}").status_code == 404
    finally:
        with store.tx(system=True) as tx:
            tx.delete_tenant(friend["id"])


def test_match_writes():
    m = ev.match_writes([{"type": "task", "title": "Call Jim"}, {"type": "person", "title": "Jim"}],
                        [{"type": "task", "title": "Call Jim at 2pm"}, {"type": "note", "title": "x"}])
    assert m["matched"] == 1 and len(m["missed"]) == 1 and len(m["extra"]) == 1 and not m["same_types"]


def test_users_page_owner_only_counts_no_content(store, settings, clean):  # noqa: F811
    seed(store, settings)
    t = owner_tenant(store)
    with store.tx(system=True) as tx:
        friend = tx.create_tenant(telegram_user_id=777002, telegram_chat_id=777002, display_name="Ravi",
                                  timezone="Asia/Kolkata")
        tx.add_inbox(friend["id"], source="whatsapp", raw_content="my private diary entry")
    try:
        app = web.create_app(store, settings)
        c, _ = sign_in(app, store, settings, t)
        page = c.get("/cohort").get_data(as_text=True)
        assert "Ravi" in page and "private diary" not in page and "style=" not in page
        assert 'href="/cohort"' in c.get("/").get_data(as_text=True)
        c2, _ = sign_in(app, store, settings, friend)
        assert c2.get("/cohort").status_code == 404
        assert 'href="/cohort"' not in c2.get("/").get_data(as_text=True)
    finally:
        with store.tx(system=True) as tx:
            tx.delete_tenant(friend["id"])
