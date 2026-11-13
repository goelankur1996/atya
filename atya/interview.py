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

MAX_TURNS = 10
EXPIRE_AFTER = dt.timedelta(hours=24)
STOP_WORDS = {"stop", "done", "later", "finish", "end", "that's all", "thats all", "enough", "no more"}
SKIP_WORDS = {"skip", "pass", "next", "skip it", "no idea", "not sure"}

INTRO = ("Let's set you up so I'm useful from day one. I'll ask about {n} quick questions; answer each "
         "with a voice note (easiest) or text, as much or as little as you like. Everything goes into your "
         "records. Say \"skip\" to skip one, or \"stop\" to finish any time.")
FIRST_QUESTION = "First: what are you working on these days, at work and outside it?"


def key(tenant_id: uuid.UUID | str) -> str:
    return f"interview:{tenant_id}"


def begin(tx: Tx, tenant_id: uuid.UUID) -> str:
    """Start (or restart) the interview. Returns the message to send."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    tx.set_state(key(tenant_id), {"turns": [{"q": FIRST_QUESTION}], "started": now, "asked": now, "inbox": []})
    return f"{INTRO.format(n=MAX_TURNS)}\n\n{FIRST_QUESTION}"


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
