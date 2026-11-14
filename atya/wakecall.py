"""The morning wake-up call (owner only): ATYA rings at a set time, goes over the day,
listens, and saves what was said.

- The time lives in tenants.wakecall_time (local), set with /wakecall. NULL = off.
- Not answered: two more tries, RETRY_MINUTES apart; then the text brief instead.
- No call more than LATE_LIMIT after the set time (so a restart at noon doesn't ring).
- /callme asks for a call now (picked up within CHECK_SECONDS).
- During the call the voice model can look things up in ATYA (`lookup`) and queue
  research for afterwards (`follow_up`). It has no live data of its own.
- After the call the transcript goes into the inbox (source 'call'); the processor
  files tasks and notes from it and replies on Telegram with what it saved.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Awaitable, Callable

from . import briefs
from .config import Settings
from .schema import load_schema
from .store import Store

log = logging.getLogger(__name__)

CHECK_SECONDS = 20
RETRY_MINUTES = 10
ATTEMPTS = 3
LATE_LIMIT = dt.timedelta(hours=2)
MAX_CALL_SECONDS = 10 * 60
DEFAULT_TIME = dt.time(6, 0)
NOW_KEY = "wakecall:now"

MISSED_NOTE = "I tried calling three times this morning. Here's the brief instead."

PROMPT = """You are ATYA, {name}'s personal assistant. You are calling him at {clock} on {day} to start his day.

How the call goes:
1. Say good morning and ask how he slept. Keep it brief and natural.
2. Go over today in a few sentences: his calendar and what's due or overdue (below). Pick what matters most;
   don't list everything, and never read reference codes like TSK-12 aloud.
3. Ask what he wants to get done today, and whether there's anything to note or to be reminded about.
4. Wrap up in a few minutes. Close by saying you'll send a summary on Telegram.

Rules:
- Speak calmly and naturally, one or two short sentences per turn. Let him lead if he wants to talk about something.
- Always speak English. Only if he speaks Hindi two or more turns in a row, switch to Hindi; switch back when he
  speaks English. {gender}
- You have no live information: no weather, news, prices, traffic or internet. Never guess at those. If he asks,
  say you can't check that on the call, and use the follow_up tool so it's researched and sent on Telegram.
- To check his own records (tasks, people, notes, his calendar on other days, anything he told ATYA before),
  use the lookup tool. Say something short like "let me check" first.
- Everything he says is saved after the call, so don't confirm each item as saved.

Today, from ATYA:
{brief}"""

GREETING = "The call just connected. Start the morning call."


def day_key(tenant_id, day: dt.date) -> str:
    return f"wakecall:{tenant_id}:{day.isoformat()}"


def owner(store: Store, settings: Settings) -> dict | None:
    if not settings.owner_telegram_user_id:
        return None
    with store.tx(system=True) as tx:
        return tx.tenant_by_telegram(settings.owner_telegram_user_id)


def morning_prompt(tenant: dict, local: dt.datetime, brief: str, voice: str) -> str:
    from .tgcall import FEMALE_VOICES
    gender = ("You are female: in Hindi always use feminine first-person forms." if voice in FEMALE_VOICES
              else "You are male: in Hindi always use masculine first-person forms.")
    return PROMPT.format(name=(tenant.get("display_name") or "him").split()[0], clock=briefs.fmt_time(local.time()),
                         day=local.strftime("%A %-d %B"), gender=gender, brief=brief)


def lookup(store: Store, llm, embedder, tenant: dict, question: str) -> str:
    """Answer a question from the user's records, the same way a typed question is answered."""
    from . import processor
    schema = load_schema(tenant["schema_name"])
    tz = processor._tz(tenant)
    now = processor.local_label(dt.datetime.now(dt.timezone.utc), tz)
    plan = llm.plan_query(schema=schema, question=question, now=now)
    vector = None
    if embedder is not None:
        try:
            vector = embedder.embed_query(question)
        except Exception:
            vector = None
    with store.tx(tenant["id"]) as tx:
        records = processor.gather_records(tx, schema, tenant["id"], plan, vector)
    return llm.answer(schema=schema, question=question, records=records, now=now)["text"]


def save_transcript(store: Store, tenant: dict, transcript: list[tuple[str, str]], started: dt.datetime) -> bool:
    """Queue the call for the processor. False if he said nothing."""
    if not any(role == "USER" and text.strip() for role, text in transcript):
        return False
    name = (tenant.get("display_name") or "Me").split()[0]
    lines = [f"{name if role == 'USER' else 'ATYA'}: {text.strip()}" for role, text in transcript if text.strip()]
    with store.tx(tenant["id"]) as tx:
        tx.add_inbox(tenant["id"], source="call", raw_content="\n".join(lines), captured_at=started,
                     sender="morning call", actor="wakecall")
    return True


def queue_follow_up(store: Store, tenant: dict, request: str) -> None:
    with store.tx(tenant["id"]) as tx:
        tx.add_inbox(tenant["id"], source="call", raw_content=f"research {request.strip()}",
                     sender="morning call", actor="wakecall")


async def morning_call(store: Store, settings: Settings, llm, embedder, tenant: dict) -> str:
    """One attempt. Returns 'answered', 'missed' or 'failed'."""
    from .sonic import Sonic
    from .tgcall import Caller, converse, up_from_24k

    user_id = tenant["telegram_user_id"]
    local = briefs.local_now(tenant["timezone"])
    with store.tx(tenant["id"]) as tx:
        brief = briefs.build_brief(tx, load_schema(tenant["schema_name"]), tenant["id"], local.date())

    async def do_lookup(args: dict) -> str:
        q = str(args.get("question", "")).strip()
        if not q:
            return "No question given."
        return await asyncio.to_thread(lookup, store, llm, embedder, tenant, q)

    async def do_follow_up(args: dict) -> str:
        req = str(args.get("request", "")).strip()
        if not req:
            return "No request given."
        await asyncio.to_thread(queue_follow_up, store, tenant, req)
        return "Queued. ATYA will research it and send the answer on Telegram after the call."

    tools = [
        {"name": "lookup", "run": do_lookup,
         "description": "Look something up in the user's own ATYA records: tasks, reminders, people, notes, "
                        "documents, and calendar events on any day. Use for any question about his own things.",
         "schema": {"type": "object", "properties": {"question": {
             "type": "string", "description": "The question, in full, e.g. 'What's on my calendar on Thursday?'"}},
                    "required": ["question"]}},
        {"name": "follow_up", "run": do_follow_up,
         "description": "Queue something to research after the call (weather, news, prices, facts, anything "
                        "needing the internet). The answer is sent on Telegram.",
         "schema": {"type": "object", "properties": {"request": {
             "type": "string", "description": "What to find out, e.g. 'weather in Noida today'"}},
                    "required": ["request"]}},
    ]

    caller = Caller(settings)
    await caller.start()
    started = dt.datetime.now(dt.timezone.utc)
    sonic = Sonic(system_prompt=morning_prompt(tenant, local, brief, settings.tg_call_voice),
                  voice=settings.tg_call_voice, region=settings.aws_region, tools=tools,
                  on_audio=lambda pcm: caller.out.extend(up_from_24k(pcm)), on_interrupt=caller.out.clear)
    try:
        try:
            await caller.ring(user_id)
        except Exception as e:
            log.info("wake-up call not answered: %s", type(e).__name__)
            return "missed"
        try:
            await sonic.open()
        except Exception:
            log.exception("voice model unavailable; hanging up")
            await caller.hang_up(user_id)
            return "failed"
        await converse(caller, sonic, user_id, greeting=GREETING, max_seconds=MAX_CALL_SECONDS)
        await sonic.close()
        await caller.hang_up(user_id)
        saved = save_transcript(store, tenant, sonic.transcript, started)
        log.info("wake-up call done: %d turns, %d lookups, saved=%s", len(sonic.transcript),
                 len(sonic.tool_calls), saved)
        return "answered"
    finally:
        await caller.stop()


def send_brief_instead(store: Store, tenant: dict, day: dt.date) -> None:
    with store.tx(tenant["id"]) as tx:
        body = briefs.build_brief(tx, load_schema(tenant["schema_name"]), tenant["id"], day)
        tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=f"{MISSED_NOTE}\n\n{body}",
                      actor="wakecall", kind="brief")


CallFn = Callable[[dict], Awaitable[str]]


def tick(store: Store, settings: Settings, call: CallFn, now: dt.datetime | None = None) -> str | None:
    """Make one decision: call now, retry, give up with the brief, or nothing. Returns what it did."""
    tenant = owner(store, settings)
    if tenant is None or not tenant["telegram_user_id"]:
        return None
    now = now or dt.datetime.now(dt.timezone.utc)

    with store.tx(system=True) as tx:
        asked = tx.get_state(NOW_KEY)
        if asked:
            tx.set_state(NOW_KEY, None)
    if asked:
        result = asyncio.run(call(tenant))
        return f"on-request:{result}"

    if tenant.get("wakecall_time") is None:
        return None
    local = briefs.local_now(tenant["timezone"], now)
    due = dt.datetime.combine(local.date(), tenant["wakecall_time"], tzinfo=local.tzinfo)
    if not (due <= local < due + LATE_LIMIT):
        return None
    key = day_key(tenant["id"], local.date())
    with store.tx(system=True) as tx:
        state = tx.get_state(key) or {"attempts": 0}
    if state.get("done") or (state.get("next_at") and now < dt.datetime.fromisoformat(state["next_at"])):
        return None

    result = asyncio.run(call(tenant))
    state["attempts"] += 1
    if result == "answered":
        state["done"] = "answered"
    elif state["attempts"] >= ATTEMPTS or result == "failed":
        send_brief_instead(store, tenant, local.date())
        state["done"] = f"brief after {result}"
    else:
        state["next_at"] = (now + dt.timedelta(minutes=RETRY_MINUTES)).isoformat()
    with store.tx(system=True) as tx:
        tx.set_state(key, state)
    return f"{result}:{state['attempts']}"


def run(store: Store, settings: Settings, llm, embedder) -> None:
    log.info("wake-up calls started")

    async def call(tenant: dict) -> str:
        return await morning_call(store, settings, llm, embedder, tenant)

    while True:
        try:
            did = tick(store, settings, call)
            if did:
                log.info("wake-up call: %s", did)
        except Exception:
            log.exception("wake-up call tick failed")
        time.sleep(CHECK_SECONDS)
