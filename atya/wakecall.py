"""The morning wake-up call: ATYA rings at a set time, goes over the day, listens,
and saves what was said. Calls come from a second Telegram account (unofficial
user-account automation), so this is an opt-in beta for a few Telegram users.

- The time lives in tenants.wakecall_time (local), set with /wakecall. NULL = off.
- Users other than the owner agree first (/wakecall agree, tenants.wakecall_consent_at);
  at most settings.call_beta_users of them; settings.daily_call_minutes each a day.
- Not answered: two more tries, RETRY_MINUTES apart; then the text brief instead.
- No call more than LATE_LIMIT after the set time (so a restart at noon doesn't ring).
- /callme asks for a call now (picked up within CHECK_SECONDS). Calls run one at a time.
- During the call the voice model can look things up in ATYA (`lookup`) and queue
  research for afterwards (`follow_up`). It has no live data of its own.
- After the call the transcript goes into the inbox (source 'call'); the processor
  files tasks and notes from it and replies with what it saved.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Awaitable, Callable

from . import briefs, limits
from .config import Settings
from .schema import load_schema
from .store import Store

log = logging.getLogger(__name__)

CHECK_SECONDS = 20
RETRY_MINUTES = 10
ATTEMPTS = 3
LATE_LIMIT = dt.timedelta(hours=2)
MAX_CALL_SECONDS = 10 * 60
MIN_CALL_SECONDS = 60
DEFAULT_TIME = dt.time(6, 0)
CALLER_KEY = "wakecall:caller"           # the calling account's name and @username, for telling users

MISSED_NOTE = "I tried calling three times this morning. Here's the brief instead."
OUT_OF_MINUTES = "You've used today's call minutes, so here's the brief instead of a call."

PROMPT = """You are ATYA, {name}'s personal assistant. You are calling {name} at {clock} on {day}.

How the call goes:
1. Greet {name} for the time of day and ask how they're doing (in the morning: how they slept). Keep it brief.
2. Go over today in a few sentences: their calendar and what's due or overdue (below). Pick what matters most;
   don't list everything, and never read reference codes like TSK-12 aloud.
3. Ask what they want to get done today, and whether there's anything to note or to be reminded about.
4. Wrap up in a few minutes. Close by saying you'll send a summary in the chat.

Rules:
- Speak calmly and naturally, one or two short sentences per turn. Let them lead if they want to talk about something.
- Always speak English. Only if they speak Hindi two or more turns in a row, switch to Hindi; switch back when they
  speak English. {gender}
- You have no live information: no weather, news, prices, traffic or internet. Never guess at those. If asked,
  say you can't check that on the call, and use the follow_up tool so it's researched and sent in the chat.
- To check their own records (tasks, people, notes, the calendar on other days, anything they told ATYA before),
  use the lookup tool. Say something short like "let me check" first.
- Everything said on the call is saved afterwards, so don't confirm each item as saved.

Today, from ATYA:
{brief}"""

GREETING = "The call just connected. Start the call."


def day_key(tenant_id, day: dt.date) -> str:
    return f"wakecall:{tenant_id}:{day.isoformat()}"


def now_key(tenant_id) -> str:
    return f"wakecall:now:{tenant_id}"


def first_name(tenant: dict) -> str:
    return (tenant.get("display_name") or "there").split()[0]


def morning_prompt(tenant: dict, local: dt.datetime, brief: str, voice: str) -> str:
    from .tgcall import FEMALE_VOICES
    gender = ("You are female: in Hindi always use feminine first-person forms." if voice in FEMALE_VOICES
              else "You are male: in Hindi always use masculine first-person forms.")
    return PROMPT.format(name=first_name(tenant), clock=briefs.fmt_time(local.time()),
                         day=local.strftime("%A %-d %B"), gender=gender, brief=brief)


def seconds_left(store: Store, settings: Settings, tenant: dict) -> int:
    """How long this user's next call may last today."""
    if limits.is_owner(tenant, settings) or not settings.daily_call_minutes:
        return MAX_CALL_SECONDS
    day = briefs.local_now(tenant["timezone"]).date()
    with store.tx(tenant["id"]) as tx:
        used = tx.usage_on(tenant["id"], day).get("call_seconds", 0)
    return max(0, min(MAX_CALL_SECONDS, settings.daily_call_minutes * 60 - used))


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
    """Queue the call for the processor. False if the user said nothing."""
    if not any(role == "USER" and text.strip() for role, text in transcript):
        return False
    name = (tenant.get("display_name") or "Me").split()[0]
    lines = [f"{name if role == 'USER' else 'ATYA'}: {text.strip()}" for role, text in transcript if text.strip()]
    with store.tx(tenant["id"]) as tx:
        tx.add_inbox(tenant["id"], source="call", raw_content="\n".join(lines), captured_at=started,
                     sender="call", actor="wakecall")
    return True


def queue_follow_up(store: Store, tenant: dict, request: str) -> None:
    with store.tx(tenant["id"]) as tx:
        tx.add_inbox(tenant["id"], source="call", raw_content=f"research {request.strip()}",
                     sender="call", actor="wakecall")


def record_call(store: Store, tenant: dict, seconds: float) -> None:
    day = briefs.local_now(tenant["timezone"]).date()
    with store.tx(tenant["id"]) as tx:
        tx.add_usage(tenant["id"], day, call_seconds=int(seconds))


async def morning_call(store: Store, settings: Settings, llm, embedder, tenant: dict) -> str:
    """One attempt. Returns 'answered', 'missed', 'unreachable', 'no-minutes' or 'failed'."""
    from .sonic import Sonic
    from .tgcall import Caller, converse, up_from_24k

    budget = seconds_left(store, settings, tenant)
    if budget < MIN_CALL_SECONDS:
        return "no-minutes"
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
        return "Queued. ATYA will research it and send the answer in the chat after the call."

    tools = [
        {"name": "lookup", "run": do_lookup,
         "description": "Look something up in the user's own ATYA records: tasks, reminders, people, notes, "
                        "documents, and calendar events on any day. Use for any question about their own things.",
         "schema": {"type": "object", "properties": {"question": {
             "type": "string", "description": "The question, in full, e.g. 'What's on my calendar on Thursday?'"}},
                    "required": ["question"]}},
        {"name": "follow_up", "run": do_follow_up,
         "description": "Queue something to research after the call (weather, news, prices, facts, anything "
                        "needing the internet). The answer is sent in the chat.",
         "schema": {"type": "object", "properties": {"request": {
             "type": "string", "description": "What to find out, e.g. 'weather in Noida today'"}},
                    "required": ["request"]}},
    ]

    caller = Caller(settings)
    await caller.start()
    try:
        with store.tx(system=True) as tx:
            tx.set_state(CALLER_KEY, await caller.identity())
    except Exception:
        log.debug("couldn't record the caller's identity", exc_info=True)
    started = dt.datetime.now(dt.timezone.utc)
    sonic = Sonic(system_prompt=morning_prompt(tenant, local, brief, settings.tg_call_voice),
                  voice=settings.tg_call_voice, region=settings.aws_region, tools=tools,
                  on_audio=lambda pcm: caller.out.extend(up_from_24k(pcm)), on_interrupt=caller.out.clear)
    try:
        try:
            await caller.ring(user_id, username=tenant.get("telegram_username"))
        except LookupError as e:
            log.info("wake-up call: %s", e)
            return "unreachable"
        except Exception as e:
            log.info("wake-up call not answered: %s", type(e).__name__)
            return "missed"
        answered = time.monotonic()
        try:
            await sonic.open()
        except Exception:
            log.exception("voice model unavailable; hanging up")
            await caller.hang_up(user_id)
            return "failed"
        await converse(caller, sonic, user_id, greeting=GREETING, max_seconds=budget)
        await sonic.close()
        await caller.hang_up(user_id)
        record_call(store, tenant, time.monotonic() - answered)
        saved = save_transcript(store, tenant, sonic.transcript, started)
        log.info("wake-up call done (%s): %d turns, %d lookups, saved=%s", str(tenant["id"])[:4],
                 len(sonic.transcript), len(sonic.tool_calls), saved)
        return "answered"
    finally:
        await caller.stop()


def tell(store: Store, tenant: dict, body: str, *, kind: str | None = None) -> None:
    with store.tx(tenant["id"]) as tx:
        tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=body, actor="wakecall", kind=kind)


def send_brief_instead(store: Store, tenant: dict, day: dt.date, note: str = MISSED_NOTE) -> None:
    with store.tx(tenant["id"]) as tx:
        body = briefs.build_brief(tx, load_schema(tenant["schema_name"]), tenant["id"], day)
    tell(store, tenant, f"{note}\n\n{body}", kind="brief")


def unreachable_note(store: Store) -> str:
    with store.tx(system=True) as tx:
        who = tx.get_state(CALLER_KEY) or {}
    handle = f"@{who['username']}" if who.get("username") else (who.get("name") or "the ATYA caller account")
    return (f"I couldn't call you: Telegram doesn't let the caller account reach you yet. Send any message to "
            f"{handle} on Telegram once, then try /callme again.")


CallFn = Callable[[dict], Awaitable[str]]


def tick(store: Store, settings: Settings, call: CallFn, now: dt.datetime | None = None) -> str | None:
    """Make at most one call (requested first, then scheduled). Returns what it did."""
    now = now or dt.datetime.now(dt.timezone.utc)
    with store.tx(system=True) as tx:
        asked = tx.state_keys("wakecall:now:")
        for key in asked[:1]:
            tx.set_state(key, None)
        tenant = tx.tenant(asked[0].rsplit(":", 1)[1]) if asked else None
    if tenant is not None and tenant["status"] == "active" and tenant["telegram_user_id"]:
        result = asyncio.run(call(tenant))
        if result == "unreachable":
            tell(store, tenant, unreachable_note(store))
        elif result == "no-minutes":
            tell(store, tenant, f"You've used today's {settings.daily_call_minutes} call minutes. "
                                "Calls work again tomorrow.")
        elif result == "failed":
            tell(store, tenant, "The call didn't go through on my side. Try /callme again in a few minutes.")
        return f"on-request:{result}"

    with store.tx(system=True) as tx:
        candidates = tx.wakecall_tenants()
    for tenant in candidates:
        local = briefs.local_now(tenant["timezone"], now)
        due = dt.datetime.combine(local.date(), tenant["wakecall_time"], tzinfo=local.tzinfo)
        if not (due <= local < due + LATE_LIMIT):
            continue
        key = day_key(tenant["id"], local.date())
        with store.tx(system=True) as tx:
            state = tx.get_state(key) or {"attempts": 0}
        if state.get("done") or (state.get("next_at") and now < dt.datetime.fromisoformat(state["next_at"])):
            continue
        result = asyncio.run(call(tenant))
        state["attempts"] += 1
        if result == "answered":
            state["done"] = "answered"
        elif result == "no-minutes":
            send_brief_instead(store, tenant, local.date(), OUT_OF_MINUTES)
            state["done"] = "brief: no minutes"
        elif result == "unreachable":
            tell(store, tenant, unreachable_note(store))
            send_brief_instead(store, tenant, local.date(), "Here's this morning's brief.")
            state["done"] = "brief: unreachable"
        elif state["attempts"] >= ATTEMPTS or result == "failed":
            send_brief_instead(store, tenant, local.date())
            state["done"] = f"brief after {result}"
        else:
            state["next_at"] = (now + dt.timedelta(minutes=RETRY_MINUTES)).isoformat()
        with store.tx(system=True) as tx:
            tx.set_state(key, state)
        return f"{result}:{state['attempts']}"
    return None


async def call_in_own_process(tenant: dict) -> str:
    """Run one call as `python -m atya.wakecall call <id>`: the calling library and the voice stream get
    a process of their own (as the terminal test does), and a crash can't touch the other workers."""
    import sys
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-m", "atya.wakecall", "call", str(tenant["telegram_user_id"]),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    result = "failed"

    async def relay():
        nonlocal result
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line.startswith("RESULT: "):
                result = line.split(": ", 1)[1].strip()
            elif line:
                log.info("[call] %s", line)
    try:
        await asyncio.wait_for(asyncio.gather(relay(), proc.wait()), timeout=MAX_CALL_SECONDS + 180)
    except asyncio.TimeoutError:
        proc.kill()
        log.error("call process for %s took too long; killed", str(tenant["id"])[:4])
        return "failed"
    return result


def run(store: Store, settings: Settings, llm, embedder) -> None:
    log.info("wake-up calls started")
    call = call_in_own_process

    while True:
        try:
            did = tick(store, settings, call)
            if did:
                log.info("wake-up call: %s", did)
        except Exception:
            log.exception("wake-up call tick failed")
        time.sleep(CHECK_SECONDS)


def main(argv: list[str]) -> int:
    """python -m atya.wakecall call [telegram_user_id]: run one morning call now, in the foreground."""
    import sys
    from .config import load_settings
    from .llm import BedrockLLM, ClaudeLLM
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    if argv[:1] != ["call"]:
        print(main.__doc__)
        return 2
    settings = load_settings()
    store = Store(settings.database_url)
    llm = (BedrockLLM(fast_model=settings.bedrock_fast_model, main_model=settings.bedrock_main_model,
                      region=settings.aws_region) if settings.llm_provider == "bedrock"
           else ClaudeLLM(settings.anthropic_api_key))
    from . import embeddings
    embedder = embeddings.VoyageEmbedder(settings.voyage_api_key) if settings.voyage_api_key else None
    uid = int(argv[1]) if len(argv) > 1 else settings.owner_telegram_user_id
    with store.tx(system=True) as tx:
        tenant = tx.tenant_by_telegram(uid)
    if tenant is None:
        print(f"No account for Telegram user {uid}")
        return 1
    try:
        result = asyncio.run(morning_call(store, settings, llm, embedder, tenant))
    except Exception:
        logging.getLogger(__name__).exception("call failed")
        result = "failed"
    print(f"RESULT: {result}", flush=True)
    return 0 if result == "answered" else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
