"""Spending caps: per-user daily limits, the all-users cap, /usage."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from atya import commands, limits, processor, receiver, sender
from atya.config import Settings
from tests.test_pipeline import OWNER, FakeLLM, FakeTelegram, owner_tenant

FRIEND = 880001


@pytest.fixture
def settings(db_url):
    return Settings(database_url=db_url, telegram_bot_token="x", anthropic_api_key="x",
                    owner_telegram_user_id=OWNER, owner_timezone="Asia/Kolkata", max_attempts=2,
                    daily_messages=2, daily_tokens=1000, daily_tokens_all=5000)


@pytest.fixture
def clean(store):
    with store.tx(system=True) as tx:
        for t in tx._all("SELECT id FROM tenants WHERE telegram_user_id = ANY(%s)", ([OWNER, FRIEND],)):
            tx.delete_tenant(t["id"])
        tx.conn.execute("DELETE FROM service_state")
    yield


class CountingLLM(FakeLLM):
    """FakeLLM that reports token use like the real transports."""

    def __init__(self, per_call=100):
        super().__init__()
        self.usage = {}
        self.per_call = per_call

    def classify(self, text):
        u = self.usage.setdefault("m", {"calls": 0, "in": 0, "out": 0, "secs": 0})
        u["in"] += self.per_call
        u["out"] += 10
        return {"intent": "chat", "reply": "ok"}


def friend(store):
    with store.tx(system=True) as tx:
        return tx.create_tenant(telegram_user_id=FRIEND, telegram_chat_id=FRIEND, timezone="Asia/Kolkata")


def run(store, tg, llm, settings):
    receiver.poll_once(store, tg, settings, timeout=0)
    while processor.process_once(store, llm, settings):
        pass
    sender.send_once(store, tg)


def test_daily_message_cap_holds_and_tells_once(store, settings, clean):
    t = friend(store)
    tg, llm = FakeTelegram(), CountingLLM()
    for i in range(4):
        tg.push(f"hi {i}", user_id=FRIEND, chat_id=FRIEND)
    run(store, tg, llm, settings)
    texts = [m[1] for m in tg.sent]
    assert texts.count("ok") == 2
    assert sum("today's limit of 2 messages a day" in x for x in texts) == 1     # told once
    with store.tx(t["id"]) as tx:
        held = tx._all("SELECT * FROM inbox WHERE status = 'failed' ORDER BY seq")
        day = dt.datetime.now(dt.timezone.utc).astimezone(processor._tz(t)).date()
        u = tx.usage_on(t["id"], day)
    assert len(held) == 2 and "daily limit" in held[0]["processor_notes"]
    assert u["messages"] == 2 and u["held"] == 2 and u["tokens_in"] == 200 and u["tokens_out"] == 20


def test_token_cap(store, settings, clean):
    friend(store)
    tg, llm = FakeTelegram(), CountingLLM(per_call=990)
    for i in range(2):
        tg.push(f"long {i}", user_id=FRIEND, chat_id=FRIEND)
    run(store, tg, llm, replace(settings, daily_messages=0))
    assert "today's processing allowance" in tg.sent[-1][1]


def test_owner_is_exempt_but_counted(store, settings, clean):
    tg, llm = FakeTelegram(), CountingLLM()
    tg.push("/start")
    for i in range(4):
        tg.push(f"hi {i}")
    run(store, tg, llm, settings)
    assert [m[1] for m in tg.sent].count("ok") == 4
    t = owner_tenant(store)
    ctx = commands.Ctx(store=store, tg=tg, settings=settings, user_id=OWNER, chat_id=555, name=None, tenant=t)
    commands.handle(ctx, "/usage")
    assert "Messages: 4" in tg.sent[-1][1] and "no limits as the owner" in tg.sent[-1][1]
    assert "Everyone today (UTC): 440 of 5,000 tokens" in tg.sent[-1][1]


def test_all_users_cap_holds_others_and_tells_owner(store, settings, clean):
    tg, llm = FakeTelegram(), CountingLLM(per_call=2990)
    tg.push("/start")
    tg.push("big 1")
    tg.push("big 2")
    run(store, tg, llm, settings)                    # owner uses 6,000 tokens: over the 5,000 cap
    friend(store)
    tg.push("hello", user_id=FRIEND, chat_id=FRIEND)
    tg.push("again", user_id=FRIEND, chat_id=FRIEND)
    run(store, tg, llm, settings)
    to_friend = [m[1] for m in tg.sent if m[0] == FRIEND]
    to_owner = [m[1] for m in tg.sent if m[0] == 555]
    assert len(to_friend) == 1 and "at capacity for today" in to_friend[0]
    assert sum("all-users processing cap" in x for x in to_owner) == 1


def test_friend_usage_command(store, settings, clean):
    t = friend(store)
    tg = FakeTelegram()
    ctx = commands.Ctx(store=store, tg=tg, settings=settings, user_id=FRIEND, chat_id=FRIEND, name=None, tenant=t)
    commands.handle(ctx, "/usage")
    assert "Messages: 0 of 2" in tg.sent[-1][1] and "reset at midnight" in tg.sent[-1][1]


def test_over_reasons():
    s = Settings(database_url="x", telegram_bot_token="x", anthropic_api_key="x", owner_telegram_user_id=1,
                 daily_messages=0, daily_tokens=0, daily_voice_minutes=1, daily_pdf_pages=5)
    u = {"messages": 99, "tokens_in": 10**9, "tokens_out": 0, "voice_seconds": 60, "pdf_pages": 5}
    assert limits.over(u, s) is None
    assert limits.over(u, s, voice=True) == "1 minute of voice notes a day"
    assert limits.over(u, s, pdf=True) == "5 PDF pages a day"
