"""The setup interview: a short conversation right after sign-up (or /setup).

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

INTRO = ("Let's set you up so I'm useful from day one: about 10 minutes, one question at a time. Answer each "
         "with a voice note (easiest) or text, as much as you like; the more you tell me, the more I can "
         "remind you of. Everything goes into your records. Say \"skip\" to skip one, or \"stop\" to "
         "finish any time.")
FIRST_QUESTION = "First, what's on for you this week? Meetings, deadlines, calls, anything with a day attached."


def key(tenant_id: uuid.UUID | str) -> str:
    return f"interview:{tenant_id}"


def begin(tx: Tx, tenant_id: uuid.UUID) -> str:
    """Start (or restart) the interview. Returns the message to send."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    tx.set_state(key(tenant_id), {"turns": [{"q": FIRST_QUESTION, "area": "week"}], "started": now, "asked": now,
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
        lines.append("Every morning I'll send you a short brief of what's due (change it with /brief). "
                     "From here on, just message me anything worth remembering. /help lists everything.")
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
