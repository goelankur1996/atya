"""Phone calls from ATYA: real phone calls through Plivo (plivo.py + callbridge.py) to Indian
numbers, otherwise through a second Telegram account. Users opt in first (/wakecall agree).

Every call is a row in the `calls` table, placed by one worker, one at a time:
- morning: every day at tenants.wakecall_time (local), set with /wakecall
- reminder: a task with Remind By = call, queued by the reminders worker when due
  (asked for in chat, "call me at 4 to ...", or on a call with the schedule_call tool)
- now: /callme
- setup: the setup interview, by call (see interview.py)

Each call retries on its own: not answered -> up to ATTEMPTS tries RETRY_MINUTES apart;
declined -> no more tries for that call (other calls go ahead). When a call doesn't
happen, the content arrives as a message instead (the brief, or the reminder).
A call is never placed more than LATE_LIMIT after its time.

During a call the voice model has one tool for the user's records, `atya`: what the user
asks or tells it goes into the inbox like a text message and is handled by the same processor
(reply spoken on the call). The other two tools, save_city and set_morning_call, are the same
functions as /city and /wakecall. The weather is fetched before dialling. After the call the
transcript is kept (not filed again) and the chat gets a summary of what changed.
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

CHECK_SECONDS = 15
RETRY_MINUTES = 10
ATTEMPTS = 3
LATE_LIMIT = dt.timedelta(hours=2)
MAX_CALL_SECONDS = 15 * 60
MIN_CALL_SECONDS = 60
DEFAULT_TIME = dt.time(6, 0)
CALLER_KEY = "wakecall:caller"           # the calling account's name and @username, for telling users

MISSED_MORNING = "I couldn't reach you on the phone this morning. Here's the brief instead."
DECLINED_MORNING = "No call then. Here's today's brief."
OUT_OF_MINUTES = "You've used today's call minutes, so here's the brief instead of a call."

RULES = """Rules for the whole call:
- One thing per turn, in one or two short sentences. After you ask a question, stop talking and wait for the answer.
- Speak calmly and warmly. Let them lead.
- This is a voice call: say "go over" or "tell you", never "see" or "show".
- If they say hello or talk over your opening, don't repeat the whole greeting: say "Hi, it's ATYA" and go on with
  your question.
- {language} {gender}
- ATYA (the atya tool) holds all their records and does everything with them. Whenever they ask about their own
  things (tasks, people, birthdays, notes, routines, the calendar on any day), want something saved or changed (add,
  edit, move, copy, mark done, delete, a reminder, "call me at 4 to ..."), report a routine day, or mention
  something worth keeping (a plan, a date, a person's details), send it with atya, in their words as a complete
  message with dates written out ("tasks on Thursday 2026-11-19"). Then tell them ATYA's reply in a few words.
  Questions the records below already answer (what's on a day, what's due, someone's birthday), answer
  straight away without atya. Use atya for anything not there or that needs more detail, and for every change.
  After a change, don't trust the records below for that item. Never say you can't make a change, never ask them
  to do it themselves. If the reply says it's still working, say it'll be in the chat and go on.
- Before each atya call, say one short filler so they aren't left in silence. Use a different one each time, never
  the same one twice in a call, for example: {fillers}
- You have no live information except the weather given below: no news, prices, traffic, AQI or internet. Never
  guess at those. Send "research <what they want>" with atya; the answer comes in the chat.
- Talk about tasks and records by name only, in plain words. Never say reference codes (like TSK-12), statuses,
  priority codes (like P1) or app words like "inbox", even if ATYA's reply has them.
- When they're done, say a short goodbye and that a summary will be in the chat."""

FILLERS = ["Let me check.", "One moment.", "Give me a second.", "Checking now.", "Let me look.", "Just a sec.",
           "Hang on.", "Let me pull that up.", "Looking it up.", "Bear with me a moment.", "Let me see.",
           "One sec, checking.", "Let me find that.", "Okay, checking.", "Sure, one moment.", "Right, let me look.",
           "Let me have a look.", "Give me a moment.", "Checking your list.", "Let me go through it.",
           "Pulling it up.", "Hmm, let me check.", "Just a moment.", "Let me dig that out.", "Okay, one second.",
           "Let me check your notes.", "Looking now.", "Let me quickly check.", "Sure, let me see.",
           "One moment, please.", "Let me look into it.", "Okay, let me find it.", "Checking that for you.",
           "Let me just check.", "Hold on a second.", "Searching now.", "Let me see what's there.",
           "A moment, please.", "Let me confirm.", "Okay, looking.", "Let me check on that.", "Right, one sec.",
           "Let me take a look.", "Give me a sec.", "Let me check the details."]

OPENING_KNOWN_CITY = """Open with ONE short turn: greet {name} by name, say the time ({clock}), give the weather in a few
words ({weather}), and ask one light, cheerful question: what would they like to focus on today: their tasks, today's
birthdays and events, or anything else? Then stop and wait.
Don't run through the day unprompted. Use the information below only for what they choose or ask about."""

OPENING_NO_CITY = """Open with ONE short turn: greet {name} by name, say the time ({clock}), and ask which city they're in,
so you can tell them the weather. Then stop and wait. When they answer, call save_city with it; it returns the
weather, which you then give in a few words. Then ask one light, cheerful question: what would they like to focus on
today: their tasks, today's birthdays and events, or anything else? Stop and wait.
Don't run through the day unprompted. Use the information below only for what they choose or ask about."""

LANGUAGE_RULES = {
    "english": "Always speak English. Only if they speak Hindi two or more turns in a row, switch to Hindi; switch "
               "back when they speak English.",
    "hinglish": "Speak Hinglish, the way people in North India talk at home: mostly Hindi, with the English words "
                "people normally use (exercise, protein, call, done, time). Never speak only English. Keep it simple, "
                "warm and respectful (aap, ji). If they clearly prefer English, follow them.",
    "hindi": "Speak simple, everyday Hindi, warm and respectful (aap, ji), with English only for words people normally "
             "say in English. If they clearly prefer English, follow them.",
}

HINGLISH_FILLERS = ["Ek second.", "Ek minute ji.", "Bas ek pal.", "Haan, check karte hain.", "Zara dekhte hain.",
                    "Ruko, abhi batate hain.", "Ek second, dekh rahe hain.", "Haan ji, ek minute.",
                    "Achha, check karte hain.", "Thoda sa ruko.", "Bas abhi.", "Dekhte hain zara.",
                    "Haan, ek pal.", "Ek second ji.", "Theek hai, dekhte hain."]

OPENING_CHECKIN = """You are calling {name} because {requester} asked ATYA to check in with them: "{about}".
Open with ONE short turn: greet {name} warmly by name, say {requester} asked you to check in, and ask the question.
Then stop and wait. If it's done, be genuinely pleased and say so. If it isn't, encourage them kindly: ask what got
in the way and suggest one small, concrete next step (when and how), without nagging or lecturing. If they say no
firmly, accept it gracefully. Before the goodbye, say that {requester} will get a short note on how it went.
Keep the call short (two or three minutes) unless they want to talk; they can also ask you about their own day."""

OPENING_REMINDER = """You are calling because {name} asked to be reminded: "{about}".
Open with ONE short turn: greet {name} by name and say the reminder in your first sentence. Then ask if they want
anything about it: another call later, or a note. Stop and wait. Keep the call short unless they want to talk."""

PROMPT = """You are ATYA, {name}'s personal assistant, on a phone call with {name}. It is {clock} on {day} ({iso} local time).

{opening}

{rules}

From ATYA (use only when relevant):
{brief}{extra}

Their records as of the start of this call (use only when relevant):
{records}"""

GREETING = "The call just connected. Begin."


def first_name(tenant: dict) -> str:
    return (tenant.get("display_name") or "there").split()[0]


def gender_rule(voice: str) -> str:
    from .tgcall import FEMALE_VOICES
    return ("You are female: in Hindi always use feminine first-person forms." if voice in FEMALE_VOICES
            else "You are male: in Hindi always use masculine first-person forms.")


def filler_list(language: str = "english") -> str:
    """All the fillers, shuffled per call so the model doesn't always reach for the first few."""
    import random
    pool = HINGLISH_FILLERS if language in ("hinglish", "hindi") else FILLERS
    return " / ".join(f'"{f}"' for f in random.sample(pool, len(pool)))


def voice_for(settings: Settings, tenant: dict) -> str:
    """The configured voice for English calls; the Hindi voice of the same gender for Hindi/Hinglish."""
    from .tgcall import FEMALE_VOICES
    if tenant.get("call_language") in ("hinglish", "hindi"):
        return "kiara" if settings.tg_call_voice in FEMALE_VOICES else "arjun"
    return settings.tg_call_voice


def build_prompt(tenant: dict, call: dict, local: dt.datetime, brief: str, voice: str,
                 weather: str | None, extra: str = "", records: str = "") -> str:
    name = first_name(tenant)
    language = tenant.get("call_language") or "english"
    clock = briefs.fmt_time(local.time())
    if call["kind"] == "reminder":
        opening = OPENING_REMINDER.format(name=name, about=call.get("about") or "something")
    elif call["kind"] == "checkin":
        opening = OPENING_CHECKIN.format(name=name, requester=call.get("requester_name") or "someone",
                                         about=call.get("about") or "how they're doing")
    elif call["kind"] == "setup":
        from . import interview
        opening = interview.CALL_OPENING.format(name=name)
    elif weather or tenant.get("city"):
        opening = OPENING_KNOWN_CITY.format(name=name, clock=clock,
                                            weather=weather or "weather unavailable today: skip it")
    else:
        opening = OPENING_NO_CITY.format(name=name, clock=clock)
    return PROMPT.format(name=name, clock=clock, day=local.strftime("%A %-d %B"),
                         iso=local.strftime("%Y-%m-%dT%H:%M"), opening=opening,
                         rules=RULES.format(gender=gender_rule(voice), fillers=filler_list(language),
                                           language=LANGUAGE_RULES.get(language, LANGUAGE_RULES["english"])),
                         brief=brief,
                         extra=f"\n\n{extra}" if extra else "", records=records or "(none loaded)")


def seconds_left(store: Store, settings: Settings, tenant: dict) -> int:
    """How long this user's next call may last today (a safety cap, not a count)."""
    if limits.is_owner(tenant, settings) or not settings.daily_call_minutes:
        return MAX_CALL_SECONDS
    day = briefs.local_now(tenant["timezone"]).date()
    with store.tx(tenant["id"]) as tx:
        used = tx.usage_on(tenant["id"], day).get("call_seconds", 0)
    return max(0, min(MAX_CALL_SECONDS, settings.daily_call_minutes * 60 - used))


ASK_WAIT_SECONDS = 25             # how long the call waits for ATYA's reply before saying it'll come in the chat
LATE_REPLY = ("ATYA is still working on it; the result will be sent in the chat. Tell them that in a few words "
              "and carry on.")


def ask_atya(store: Store, tenant: dict, message: str, requests: list, *, wait: float = ASK_WAIT_SECONDS,
             poll: float = 0.3) -> str:
    """What the user said on the call goes to ATYA exactly like a text message (same processor, same
    records); the reply comes back to be spoken. Too slow: the reply goes to the chat instead."""
    from .store import CALL_REQUEST
    message = message.strip()
    if not message:
        return "Nothing was sent: the message was empty."
    with store.tx(tenant["id"]) as tx:
        inbox_id = tx.add_inbox(tenant["id"], source="call", raw_content=message, sender=CALL_REQUEST,
                                actor="wakecall")
    requests.append(inbox_id)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        time.sleep(poll)
        with store.tx(tenant["id"]) as tx:
            replies = tx.call_replies(inbox_id)
        if replies:
            return "\n".join(replies)
    with store.tx(tenant["id"]) as tx:
        if tx.release_call_request(inbox_id):
            return LATE_REPLY
        return "\n".join(tx.call_replies(inbox_id)) or LATE_REPLY


def save_transcript(store: Store, tenant: dict, transcript: list[tuple[str, str]], started: dt.datetime,
                    *, sender: str = "call"):
    """Keep the call's transcript in the inbox, as a record of what was said. It isn't filed: everything
    worth keeping was sent to ATYA during the call. Returns its inbox id; None if the user said nothing."""
    if not any(role == "USER" and text.strip() for role, text in transcript):
        return None
    name = (tenant.get("display_name") or "Me").split()[0]
    lines = [f"{name if role == 'USER' else 'ATYA'}: {text.strip()}" for role, text in transcript if text.strip()]
    with store.tx(tenant["id"]) as tx:
        row_id = tx.add_inbox(tenant["id"], source="call", raw_content="\n".join(lines), captured_at=started,
                              sender=sender, actor="wakecall", status="processing")
        tx.finish_inbox(row_id, status="processed", notes="call transcript (kept as said; its requests were "
                                                         "handled during the call)")
    return row_id


def save_city(store: Store, tenant: dict, city: str, http=None) -> str:
    """The call's side of /city: the same function sets it, then today's weather to say."""
    from . import weather
    kw = {"http": http} if http else {}
    place = weather.set_city(store, tenant, city, **kw)
    if place is None:
        return "Couldn't find that city. Ask them to say it again, maybe with the state or country."
    line = weather.today_line(place["lat"], place["lon"], **kw)
    return f"Saved {place['label']}. Weather there today: {line}" if line else \
        f"Saved {place['label']}. The weather service didn't answer, so skip the weather today."


def set_morning_call(store: Store, tenant: dict, when: str, now: dt.datetime | None = None) -> str:
    """The morning call's time, or 'off'. The one function behind /wakecall <time> and the call's tool."""
    w = when.strip().lower()
    if w in ("off", "stop", "none", "no"):
        with store.tx(tenant["id"]) as tx:
            tx.set_wakecall(tenant["id"], None)
        return "Morning call turned off."
    at = DEFAULT_TIME if w in ("on", "yes", "start", "") else briefs.parse_time(when)
    if at is None:
        return "That time wasn't clear. Try a time like 6:30 or 7 am."
    local = briefs.local_now(tenant["timezone"], now)
    with store.tx(tenant["id"]) as tx:
        tx.set_wakecall(tenant["id"], at)
        if tenant.get("wakecall_consent_at") is None:
            tx.set_wakecall_consent(tenant["id"])
    first = "today"
    if local.time() >= at:                          # already past it today: start tomorrow
        with store.tx(tenant["id"]) as tx:
            skip = tx.add_call(tenant["id"], "morning", dt.datetime.now(dt.timezone.utc), day=local.date())
            if skip:
                tx.finish_call(skip["id"], "cancelled")
        first = "tomorrow"
    return f"Done: a call every morning at {briefs.fmt_time(at)} ({tenant['timezone']}), starting {first}."


def record_call(store: Store, tenant: dict, seconds: float) -> None:
    day = briefs.local_now(tenant["timezone"]).date()
    with store.tx(tenant["id"]) as tx:
        tx.add_usage(tenant["id"], day, call_seconds=int(seconds))


def call_extra(store: Store, tenant: dict, call: dict) -> str:
    """More context for the prompt: routines to check in on, the setup plan."""
    parts = []
    try:
        from . import routines
        with store.tx(tenant["id"]) as tx:
            r = routines.call_context(tx, tenant, briefs.local_now(tenant["timezone"]).date())
        if r:
            parts.append(r)
    except Exception:
        log.debug("no routine context", exc_info=True)
    if call["kind"] == "setup":
        from . import interview
        parts.append(interview.call_plan())
    return "\n\n".join(parts)


def call_tools(store: Store, tenant: dict, requests: list) -> list[dict]:
    """The call's tools. Everything about their records goes through `atya`, the same way as a text message;
    the other two are the same functions as the /city and /wakecall commands."""
    def text_tool(fn):
        async def run(args: dict) -> str:
            return await asyncio.to_thread(fn, args)
        return run

    return [
        {"name": "atya", "run": text_tool(lambda a: ask_atya(store, tenant, str(a.get("message", "")), requests)),
         "description": "Send something to ATYA exactly as if the user had texted it, and get ATYA's reply. Use it "
                        "for everything about their records: questions (tasks, people, birthdays, notes, documents, "
                        "routines, the calendar on any day), anything to save (a task, note, reminder, a person's "
                        "details), any change (mark done, move, copy, edit, delete), a reminder or another call "
                        "('call me at 4 to leave'), a routine check-in, and 'research <topic>' for anything that "
                        "needs the internet (news, prices, AQI). Changes are confirmed in the reply.",
         "schema": {"type": "object", "properties": {"message": {
             "type": "string", "description": "The message as the user would text it: complete on its own, in "
                            "their words, with dates written out, e.g. 'What's on my calendar on Thursday "
                            "2026-11-19?', 'Mark the Storytelling with Data workshop on 2026-11-19 done', "
                            "'Call me at 2026-11-17 16:00 to leave for the airport'"}},
                    "required": ["message"]}},
        {"name": "set_morning_call", "run": text_tool(lambda a: set_morning_call(store, tenant, str(a.get("time", "")))),
         "description": "Turn on (or move) the daily morning call from ATYA, at a local time, or 'off' to stop it.",
         "schema": {"type": "object", "properties": {"time": {
             "type": "string", "description": "Local time like '6:30' or '7am', or 'off'"}}, "required": ["time"]}},
        {"name": "save_city", "run": text_tool(lambda a: save_city(store, tenant, str(a.get("city", "")))),
         "description": "Save the city the user lives in (for the weather on future calls). Returns today's weather.",
         "schema": {"type": "object", "properties": {"city": {
             "type": "string", "description": "City, with state or country if given, e.g. 'Noida, India'"}},
                    "required": ["city"]}},
    ]


async def prepare(store: Store, settings: Settings, tenant: dict, call: dict,
                  requests: list) -> tuple[str, list[dict]]:
    """The voice model's instructions and tools for this call (brief, weather and context fetched now)."""
    from . import weather as wx
    local = briefs.local_now(tenant["timezone"])
    schema = load_schema(tenant["schema_name"])
    with store.tx(tenant["id"]) as tx:
        brief = briefs.build_brief(tx, schema, tenant["id"], local.date())
        records = briefs.call_records(tx, schema, tenant["id"], local.date())
    forecast = None
    if call["kind"] in ("morning", "now") and tenant.get("city_lat") is not None:
        forecast = await asyncio.to_thread(wx.today_line, tenant["city_lat"], tenant["city_lon"])
    extra = call_extra(store, tenant, call)
    if call.get("requested_by"):
        from .circle import first
        with store.tx(system=True) as tx:
            requester = tx.tenant(call["requested_by"])
        call = {**call, "requester_name": first(requester)}
    return (build_prompt(tenant, call, local, brief, voice_for(settings, tenant), forecast, extra, records),
            call_tools(store, tenant, requests))


def finish_talk(store: Store, tenant: dict, call: dict, sonic, seconds: float, started: dt.datetime,
                requests: list) -> None:
    """After an answered call: count the minutes, keep the transcript, and say in the chat what changed."""
    record_call(store, tenant, seconds)
    setup = call["kind"] == "setup"
    saved = save_transcript(store, tenant, sonic.transcript, started, sender="setup call" if setup else "call")
    if saved and call.get("id"):
        with store.tx(system=True) as tx:
            tx.set_call_transcript(call["id"], saved)
    with store.tx(tenant["id"]) as tx:
        made = [r for i in requests for r in tx.records_from_inbox(tenant["id"], i)]
        changes = [line for i in requests
                   if (tx._one("SELECT intent FROM inbox WHERE id = %s", (i,)) or {}).get("intent") == "log"
                   for body in tx.call_replies(i) for line in body.splitlines() if line.startswith("- ")]
        if setup:
            counts: dict[str, int] = {}
            for r in made:
                counts[r["type"]] = counts.get(r["type"], 0) + 1
            from . import interview
            body = interview.done_by_call(tx, tx.tenant(tenant["id"]), counts)
        elif changes:
            body = "From our call:\n" + "\n".join(changes)
        else:
            body = None
        if body:
            tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=body, actor="wakecall")
    log.info("call done (%s, %s): %d turns, %d tool uses, %d requests, transcript saved=%s", str(tenant["id"])[:4],
             call["kind"], len(sonic.transcript), len(sonic.tool_calls), len(requests), bool(saved))


async def place_call(store: Store, settings: Settings, llm, embedder, tenant: dict, call: dict) -> str:
    """One attempt on Telegram. Returns 'answered', 'missed', 'declined', 'unreachable', 'no-minutes' or 'failed'."""
    from .sonic import Sonic
    from .tgcall import Caller, converse, up_from_24k

    budget = seconds_left(store, settings, tenant)
    if budget < MIN_CALL_SECONDS:
        return "no-minutes"
    user_id = tenant["telegram_user_id"]
    requests: list = []
    prompt, tools = await prepare(store, settings, tenant, call, requests)

    caller = Caller(settings)
    await caller.start()
    try:
        with store.tx(system=True) as tx:
            tx.set_state(CALLER_KEY, await caller.identity())
    except Exception:
        log.debug("couldn't record the caller's identity", exc_info=True)
    started = dt.datetime.now(dt.timezone.utc)
    sonic = Sonic(system_prompt=prompt, voice=voice_for(settings, tenant), region=settings.aws_region, tools=tools,
                  on_audio=lambda pcm: caller.out.extend(up_from_24k(pcm)), on_interrupt=caller.out.clear)
    try:
        try:
            await caller.ring(user_id, username=tenant.get("telegram_username"))
        except LookupError as e:
            log.info("call: %s", e)
            return "unreachable"
        except Exception as e:
            name = type(e).__name__
            log.info("call not answered: %s", name)
            return "declined" if "Declined" in name or "Busy" in name else "missed"
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
        finish_talk(store, tenant, call, sonic, time.monotonic() - answered, started, requests)
        return "answered"
    finally:
        await caller.stop()


def make_models(settings: Settings):
    """The text model and embedder a call's tools need (for processes of their own)."""
    from . import embeddings
    from .llm import BedrockLLM, ClaudeLLM
    llm = (BedrockLLM(fast_model=settings.bedrock_fast_model, main_model=settings.bedrock_main_model,
                      region=settings.aws_region) if settings.llm_provider == "bedrock"
           else ClaudeLLM(settings.anthropic_api_key))
    embedder = embeddings.VoyageEmbedder(settings.voyage_api_key) if settings.voyage_api_key else None
    return llm, embedder


# --- what happens after an attempt ---------------------------------------------

def tell(store: Store, tenant: dict, body: str, *, kind: str | None = None) -> None:
    with store.tx(tenant["id"]) as tx:
        tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=body, actor="wakecall", kind=kind)


def send_brief_instead(store: Store, tenant: dict, day: dt.date, note: str) -> None:
    with store.tx(tenant["id"]) as tx:
        body = briefs.build_brief(tx, load_schema(tenant["schema_name"]), tenant["id"], day)
    tell(store, tenant, f"{note}\n\n{body}", kind="brief")


def send_reminder_instead(store: Store, tenant: dict, call: dict, note: str) -> None:
    from .reminders import queue_message
    record = None
    if call.get("record_id"):
        with store.tx(tenant["id"]) as tx:
            record = tx.record(tenant["id"], call["record_id"])
    if record is None:
        tell(store, tenant, f"⏰ {call.get('about') or 'Your reminder'}\n({note})", kind="reminder")
        return
    prefix = load_schema(tenant["schema_name"]).types["task"].prefix
    with store.tx(tenant["id"]) as tx:
        queue_message(tx, tenant["id"], tenant["telegram_chat_id"], record["id"], record["title"],
                      f"{prefix}-{record['seq']}", f"\n({note})")


def unreachable_note(store: Store) -> str:
    with store.tx(system=True) as tx:
        who = tx.get_state(CALLER_KEY) or {}
    handle = f"@{who['username']}" if who.get("username") else (who.get("name") or "the ATYA caller account")
    return (f"I couldn't call you: Telegram doesn't let the caller account reach you yet. Send any message to "
            f"{handle} on Telegram once, then /callme to try again.")


def close_call_only_task(store: Store, tenant: dict, record_id) -> None:
    """A 'Call from ATYA' task (a call with no other purpose) is done once the call happens."""
    with store.tx(tenant["id"]) as tx:
        rec = tx.record(tenant["id"], record_id)
        if rec and rec["title"] == "Call from ATYA" and rec["fields"].get("Status") not in ("done", "dropped"):
            tx.update_record(tenant["id"], load_schema(tenant["schema_name"]), record_id,
                             {"Status": "done", "Completed At": dt.date.today().isoformat()}, actor="wakecall")


def after_call(store: Store, settings: Settings, tenant: dict, call: dict, result: str,
               now: dt.datetime, llm=None) -> str:
    """The call's new status: retry (not answered), or done, with a message instead where needed."""
    kind = call["kind"]
    if result == "missed" and call["attempts"] < ATTEMPTS and kind in ("morning", "reminder", "checkin"):
        with store.tx(system=True) as tx:
            tx.finish_call(call["id"], "pending", next_at=now + dt.timedelta(minutes=RETRY_MINUTES))
        return "retry"
    if result == "answered":
        if kind == "reminder" and call.get("record_id"):
            close_call_only_task(store, tenant, call["record_id"])
    else:
        day = briefs.local_now(tenant["timezone"], now).date()
        if result == "unreachable":
            tell(store, tenant, unreachable_note(store))
        if kind == "morning" and result != "no-consent":
            note = {"declined": DECLINED_MORNING, "no-minutes": OUT_OF_MINUTES}.get(result, MISSED_MORNING)
            send_brief_instead(store, tenant, day, note)
        elif kind == "reminder":
            note = {"declined": "you declined the call", "no-minutes": "out of call minutes today",
                    "no-consent": "to get reminders as phone calls, send /wakecall first",
                    "no-line": "I couldn't call you"}.get(
                result, "I called but couldn't reach you")
            send_reminder_instead(store, tenant, call, note)
        elif kind == "setup":
            from . import interview
            interview.call_fallback(store, settings, tenant, result)
        elif kind == "now" and result not in ("unreachable", "no-consent", "no-line"):
            tell(store, tenant, {
                "declined": "Call declined. Send /callme whenever you want one.",
                "no-minutes": f"You've used today's {settings.daily_call_minutes} call minutes. They reset tomorrow.",
                "failed": "The call didn't go through on my side. Try /callme again in a few minutes.",
            }.get(result, "I called but couldn't reach you. Send /callme to try again."))
    if kind == "checkin":
        from . import circle
        with store.tx(system=True) as tx:
            fresh = tx.call(call["id"]) or call
        circle.report(store, llm, {**fresh, "attempts": call["attempts"]}, tenant, result)
    status = "cancelled" if result in ("no-consent", "no-line") else result
    with store.tx(system=True) as tx:
        tx.finish_call(call["id"], status)
    return status


CallFn = Callable[[dict, dict], Awaitable[str]]


def queue_morning_calls(store: Store, now: dt.datetime) -> int:
    """Today's morning call for everyone whose time has come (once per local day)."""
    with store.tx(system=True) as tx:
        tenants = tx.wakecall_tenants()
    n = 0
    for t in tenants:
        local = briefs.local_now(t["timezone"], now)
        due = dt.datetime.combine(local.date(), t["wakecall_time"], tzinfo=local.tzinfo)
        if not (due <= local < due + LATE_LIMIT):
            continue
        with store.tx(t["id"]) as tx:
            if tx.add_call(t["id"], "morning", due.astimezone(dt.timezone.utc), day=local.date()):
                n += 1
    return n


def tick(store: Store, settings: Settings, call_fn: CallFn, now: dt.datetime | None = None,
         llm=None) -> str | None:
    """Queue today's morning calls and check-ins, then place at most one due call. Returns what happened."""
    from . import circle
    now = now or dt.datetime.now(dt.timezone.utc)
    queue_morning_calls(store, now)
    circle.queue_checkins(store, now, llm)
    with store.tx(system=True) as tx:
        call = tx.claim_due_call(now)
        tenant = tx.tenant(call["tenant_id"]) if call else None
    if call is None:
        return None
    from .plivo import line_for
    if tenant is None or tenant["status"] not in ("active", "invited"):
        with store.tx(system=True) as tx:
            tx.finish_call(call["id"], "cancelled")
        return f"{call['kind']}:cancelled"
    if line_for(settings, tenant) is None:
        result = "no-line"                          # no way to call them (e.g. calls off, or a number we can't dial)
    elif call["kind"] == "checkin" and not circle.allowed(store, call):
        result = "no-consent"                       # they ended the circle link since it was queued
    elif call["kind"] not in ("setup", "checkin") and not tenant.get("wakecall_consent_at") \
            and not limits.is_owner(tenant, settings):
        result = "no-consent"                       # asked for a call without agreeing to calls yet
    elif now - call["due_at"] > LATE_LIMIT:
        result = "missed"
        call["attempts"] = ATTEMPTS                 # too late: no retries, just the message instead
    else:
        result = asyncio.run(call_fn(tenant, call))
    status = after_call(store, settings, tenant, call, result, now, llm)
    return f"{call['kind']}:{result}:{status}"


async def call_in_own_process(tenant: dict, call: dict) -> str:
    """Run one call as `python -m atya.wakecall call <call id>`: the calling library and the voice stream get
    a process of their own, and a crash can't touch the other workers."""
    import sys
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-m", "atya.wakecall", "call", str(call["id"]),
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


def dialer(store: Store, settings: Settings) -> CallFn:
    """Place one attempt on the user's line: a real phone call when we can, otherwise Telegram."""
    from . import plivo

    async def dial(tenant: dict, call: dict) -> str:
        line = plivo.line_for(settings, tenant)
        with store.tx(system=True) as tx:
            tx.set_call_line(call["id"], line)
        if line == "phone":
            budget = seconds_left(store, settings, tenant)
            if budget < MIN_CALL_SECONDS:
                return "no-minutes"
            return await plivo.place_call(store, settings, tenant, call, budget=budget)
        return await call_in_own_process(tenant, call)
    return dial


def run(store: Store, settings: Settings, llm, embedder) -> None:
    log.info("calls worker started (phone %s, Telegram %s)", "on" if settings.plivo_on else "off",
             "on" if settings.tg_caller_on else "off")
    with store.tx(system=True) as tx:
        stuck = tx.unstick_calls()
    if stuck:
        log.info("%d call(s) were mid-way at shutdown; queued again", stuck)
    while True:
        try:
            did = tick(store, settings, dialer(store, settings), llm=llm)
            if did:
                log.info("call: %s", did)
                continue                              # more may be due
        except Exception:
            log.exception("calls tick failed")
        time.sleep(CHECK_SECONDS)


def main(argv: list[str]) -> int:
    """python -m atya.wakecall call <call id>: place one queued call now, in the foreground."""
    import sys
    from .config import load_settings
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    if argv[:1] != ["call"] or len(argv) < 2:
        print(main.__doc__)
        return 2
    settings = load_settings()
    store = Store(settings.database_url)
    llm, embedder = make_models(settings)
    with store.tx(system=True) as tx:
        call = tx.call(argv[1])
        tenant = tx.tenant(call["tenant_id"]) if call else None
    if call is None or tenant is None:
        print(f"No such call {argv[1]}")
        print("RESULT: failed", flush=True)
        return 1
    try:
        result = asyncio.run(place_call(store, settings, llm, embedder, tenant, call))
    except Exception:
        logging.getLogger(__name__).exception("call failed")
        result = "failed"
    print(f"RESULT: {result}", flush=True)
    return 0 if result == "answered" else 1


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
