"""The setup interview: the onboarding conversation right after sign-up (or /setup).

Before it, in chat: their name, then their language (commands.onboarding_prompt).
By phone where possible ("Call me now" doubles as agreeing to calls): the call
follows CALL_OPENING + call_plan(): city and weather, what ATYA does, broad
questions, zooming in (saying why), habits, a recap. What they say is noted
through ATYA in the background; afterwards the chat lists it by name.
If they choose text, or the call doesn't happen, it's the message interview below
(the same arc: city, what ATYA does, broad questions, then deeper ones).

ATYA asks one question at a time, by text; the user answers by voice note or
text. Each answer is filed straight away like any message, and the next
question follows up on what was said (the model picks it). It ends after
MAX_TURNS answers, when the model judges the ground covered, or when the user
says "stop". "skip" skips a question. An interview left alone for a day ends
quietly, and the next message is handled normally.

State lives in service_state under interview:<tenant id>.
"""

from __future__ import annotations

import datetime as dt
import uuid

from .store import Tx

MAX_TURNS = 20             # hard stop
MIN_TURNS = 12             # don't wrap up before this many questions unless the target is met
TARGET_RECORDS = 15        # aim: this many things saved
# What a good setup covers, most time-sensitive first. The model is told what's
# still open; the interview can't end while any of it is (unless MAX_TURNS or "stop").
AREAS = {
    "week": "what's on this week: meetings, appointments, deadlines, calls (with days and times)",
    "urgent": "anything urgent or time-sensitive to be reminded about: bills, renewals, payments, forms, calls to make",
    "occasions": "birthdays, anniversaries and occasions in the next month or so (whose, which date)",
    "travel": "trips or travel coming up (where, which dates, what's left to arrange)",
    "work": "what they are working on, and the projects in it",
    "people": "the people who matter right now and what's open with each (owed, waiting on, follow-ups)",
    "goals": "goals for the next few months",
    "recurring": "recurring things: weekly meetings, habits, regular bills or chores",
    "slipping": "what has been nagging them or slipping",
}
EXPIRE_AFTER = dt.timedelta(hours=24)
STOP_WORDS = {"stop", "done", "later", "finish", "end", "that's all", "thats all", "enough", "no more"}
SKIP_WORDS = {"skip", "pass", "next", "skip it", "no idea", "not sure"}

INTRO = ("Let's set you up so I'm useful from day one. Quickly, what I do for you:\n"
         "- I remember anything you tell me, by message, voice note or call.\n"
         "- I remind you at the right moment, by message or with a call.\n"
         "- A short morning message (or call) about your day.\n"
         "- I coordinate with your friends and family: ask them things for you, or check in on them.\n\n"
         "A few questions now, one at a time, about 10 minutes. Answer by voice note (easiest) or text. "
         "Say \"skip\" to skip one, or \"stop\" to finish any time.")
FIRST_QUESTION = "First, the small stuff: which city do you live in?"
SECOND_QUESTION = ("Thanks! Now, what does a normal week look like for you? Work, home, anything you're juggling.")


def key(tenant_id: uuid.UUID | str) -> str:
    return f"interview:{tenant_id}"


def begin(tx: Tx, tenant_id: uuid.UUID) -> str:
    """Start (or restart) the interview. Returns the message to send."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    tx.set_state(key(tenant_id), {"turns": [{"q": FIRST_QUESTION, "area": "city"}], "started": now, "asked": now,
                                  "inbox": [], "covered": [], "saved": 0})
    return f"{INTRO}\n\n{FIRST_QUESTION}"


def current(tx: Tx, tenant_id: uuid.UUID) -> dict | None:
    """The interview in progress, or None. An abandoned one is ended here."""
    st = tx.get_state(key(tenant_id))
    if not st:
        return None
    if dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(st["asked"]) > EXPIRE_AFTER:
        tx.set_state(key(tenant_id), None)
        return None
    return st


def classify_reply(text: str) -> str:
    """'stop', 'skip' or 'answer'."""
    t = text.strip().lower().strip(".!? ")
    if t in STOP_WORDS:
        return "stop"
    if t in SKIP_WORDS:
        return "skip"
    return "answer"


PLURALS = {"person": "people", "library": "library items", "journal": "journal entries", "entry": "entries"}


def count_phrase(counts: dict[str, int]) -> str:
    """{"task": 2, "person": 1} -> "2 tasks, 1 person" (keys are record type keys)."""
    parts = []
    for key_, n in counts.items():
        word = key_.replace("_", " ")
        parts.append(f"{n} {word if n == 1 else PLURALS.get(key_, word + 's')}")
    return ", ".join(parts)


def summary(counts: dict[str, int], suggest: str) -> str:
    if counts:
        got = count_phrase(counts)
        head = f"That's it, thanks. I've got: {got}."
    else:
        head = "No problem, we can do this another time (send /setup)."
    lines = [head]
    if counts:
        lines.append(f"Try asking me: “{suggest or 'What do I need to do this week?'}”")
        lines.append("Would you like a short message about your day every morning? Send /brief 9am (any time works). "
                     "From here on, just message me anything worth remembering; /circle lets me ask a friend or "
                     "family member something for you. /help lists everything.")
    return "\n\n".join(lines)


def still_open(st: dict) -> list[str]:
    return [k for k in AREAS if k not in set(st.get("covered", []))]


def may_finish(st: dict) -> bool:
    """Whether the interview is allowed to end now (the model may want to sooner)."""
    asked = len(st["turns"])
    if asked >= MAX_TURNS:
        return True
    if still_open(st):
        return False
    return st.get("saved", 0) >= TARGET_RECORDS or asked >= MIN_TURNS


FALLBACK = {
    "week": "What's on for you this week? Meetings, deadlines, calls, anything with a day attached.",
    "urgent": "Anything urgent I should remind you about? Bills, renewals, payments, forms, calls to make.",
    "occasions": "Any birthdays or anniversaries coming up in the next month or so? Whose, and when?",
    "travel": "Any trips or travel coming up? Where to, and which dates?",
    "work": "What are you working on these days, at work and outside it?",
    "people": "Who are the people that matter most right now, and what's open with each of them?",
    "goals": "What are you trying to get done in the next few months?",
    "recurring": "Anything that repeats every week or month? Meetings, habits, bills, chores.",
    "slipping": "What's been nagging you or slipping lately?",
}


def fallback_question(area: str | None) -> str:
    return FALLBACK.get(area or "", "Anything else coming up that you'd like me to keep track of?")



# --- by phone -------------------------------------------------------------------

CALL_OFFER = ("Let's get you set up. The easiest way is a 10-minute call: I'll tell you what I can do for you and "
              "learn a little about your life, so I'm useful from day one. I'll ring you {how}.\n\n"
              "Tapping \"Call me now\" also means you're okay with ATYA calling you; you can turn calls off any time.")
CALL_OFFER_HI = ("Chaliye aapko set up karte hain. Sabse aasaan hai 10 minute ki ek call: main bataunga ki main aapke "
                 "liye kya kar sakta hoon, aur aapke baare mein thoda jaanunga. Main aapko {how} call karunga.\n\n"
                 "\"Call karo\" dabane ka matlab hai aap ATYA ki calls ke liye haan keh rahe hain; calls kabhi bhi band "
                 "kar sakte hain.")
CALL_BUTTONS = [[("Call me now", "setup:call"), ("Text instead", "setup:text")]]
CALL_BUTTONS_HI = [[("Call karo", "setup:call"), ("Message pe karein", "setup:text")]]
CALL_SOON = "Calling you {how} in a few seconds."
CALL_SOON_HI = "Kuch hi second mein aapko {how} call kar raha hoon."
TEXT_INSTEAD = "No problem, let's do it by message instead."

CALL_OPENING = """This is {name}'s setup call. They just joined ATYA (their personal assistant) and chose to set it up by
phone. This is their onboarding: make it feel like a friendly first conversation, not a form. It has six parts, in
order; the plan below has the rest.

Part 1, hello and the small stuff (about a minute). Open with ONE short turn: greet {name} warmly by name, say this is
a quick setup chat of about ten minutes, and {city_ask} Then stop and wait."""

CITY_ASK = ("ask which city they live in. When they answer, call save_city with it; it returns today's weather, which "
            "you mention in a few words (a small first taste of what you do).")
CITY_KNOWN = "ask how their day is going."


def call_plan() -> str:
    return f"""Setup plan, after part 1:

Part 2, what ATYA does (about a minute). In two or three short turns (this part may run a little longer than your
usual one or two sentences), tell them the four things ATYA does for them:
- Remembers anything they tell it, by message, voice note, or on a call like this one.
- Reminds them at the right moment, by message or with a call.
- A short morning message or a morning call about their day.
- Coordinates with their friends and family: asks someone something for them, or checks in on them, and passes
  the answer back.
Then ask: which of these sounds most useful to them? Use the answer to decide where to spend the rest of the call.

Part 3, broad questions (about three minutes). Ask what a normal week looks like for them: work, home, anything
they're juggling. Listen, reflect it back in one sentence, then ask one or two more broad questions: what's coming
up soon, and what's been on their mind.

Part 4, zoom in, saying why (about four minutes). Pick the two or three threads from what they said that matter most
to them, and go deeper, one question at a time. When you zoom in, sometimes say why, briefly and naturally, for
example:
- "Who's involved in that? I'll remember them, so I can remind you when something's open with them."
- "That sounds like a bigger project. If you give me the main pieces, I can break it down and keep track of each."
- "When is that due? So I can remind you at the right time, not too early and not too late."
- "Any birthdays or anniversaries coming up? I'll make sure you don't miss them."
Depth on what matters to them beats covering every topic. Don't ask everything; don't repeat a reason.

All through the call, whenever they tell you something worth keeping (a task, a date, a person and who they are, a
project and its pieces, a plan, a birthday), send it to ATYA with the atya tool as a complete message in their first
person, with expects_reply false: it's saved in the background, so don't wait, don't use a filler, and don't
announce each save; just carry on the conversation. One message per thing.

Part 5, habits (about a minute). Ask whether they'd like a short morning message about their day, or a quick morning
call like this one, and at what time. Set only what they choose: set_morning_brief for the message,
set_morning_call for the call. If family or friends came up, mention that ATYA can ask them things or check in on
them, and that the chat will say how.

Part 6, wrap up (half a minute). Recap three or four things you noted, in plain words. Then: from now on they can
just message ATYA anything, and a summary is in their chat. Say a warm goodbye.

Aim for about ten to twelve minutes in all. If they want to stop early, skip to part 6."""


def call_fallback(store, settings, tenant: dict, result: str) -> None:
    """The setup call didn't happen: do the interview by message."""
    with store.tx(tenant["id"]) as tx:
        opening = begin(tx, tenant["id"])
        note = {"declined": "No call then. ", "unreachable": ""}.get(result, "I couldn't reach you by phone. ")
        tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=f"{note}{TEXT_INSTEAD}\n\n{opening}",
                      actor="interview")


def done_by_call(tenant: dict, noted: list[str]) -> str:
    """The chat message after the setup call: what was noted (by name), what's set up, what to try."""
    from .briefs import fmt_time
    hi = tenant.get("call_language") in ("hinglish", "hindi")
    parts = [("Call ke liye shukriya! Maine yeh note kiya:" if hi else "Thanks for the call! Here's what I noted:")
             + ("\n" + "\n".join(noted) if noted else ("\n(abhi kuch nahi)" if hi else "\n(nothing yet)"))]
    habits = []
    if tenant.get("brief_time"):
        habits.append(f"a morning message at {fmt_time(tenant['brief_time'])} (/brief to change)")
    if tenant.get("wakecall_time"):
        habits.append(f"a morning call at {fmt_time(tenant['wakecall_time'])} (/wakecall to change)")
    parts.append("Set up: " + "; ".join(habits) + "." if habits else
                 "No morning message yet: send /brief 9am if you'd like one.")
    parts.append("Try asking me: \"What's due this week?\" or \"Remind me to call Mom on Sunday at 6.\" "
                 "To have me ask a friend or family member something for you, send /circle.")
    return "\n\n".join(parts)
