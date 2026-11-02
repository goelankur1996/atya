"""Claude calls: classify, plan the fan-out, plan the search, answer.

Prompts are ported from v1 (assistant_bot/claude.py) with Notion wording
replaced. The `LLM` protocol lets tests swap in a fake.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from anthropic import Anthropic

from .schema import Schema

CLASSIFIER_MODEL = "claude-haiku-4-5"
MODEL = "claude-sonnet-4-6"
MAX_TYPES_TO_SEARCH = 4


class LLMError(Exception):
    pass


class LLM(Protocol):
    # `now` / `captured_local` are human-readable local times with weekday and
    # timezone, e.g. "Tuesday 2026-11-03 00:12 (Asia/Kolkata)".
    def classify(self, text: str) -> dict: ...   # {"intent": log|query|chat, "reply": str}
    def plan_fanout(self, *, schema: Schema, raw_content: str, captured_local: str,
                    context: dict[str, list[dict]]) -> dict: ...
    def plan_query(self, *, schema: Schema, question: str, now: str) -> dict: ...
    def answer(self, *, schema: Schema, question: str, records: list[dict], now: str) -> str: ...


# --- prompts -------------------------------------------------------------------

CLASSIFY_SYSTEM = """You classify a single message sent to a personal memory bot as "log", "query" or "chat".

- log = a capture: a declarative statement, observation, event, link, person mention, commitment, decision, journal entry, reading material, training log, meal log, etc.
- query = a question or request for information ("what / who / when / where / why / how / can / should / is / are / show me / list / how many", or any other request to retrieve known data).
- chat = pure small talk addressed to the bot with nothing to save and nothing to look up: greetings, thanks, "ok", "are you there?", "test".

Tie-breakers: a message with any real information in it is "log", even if it starts with a greeting ("hi, met Rahul today" = log). If it is genuinely ambiguous between log and query, output "query".

For chat only, also write a reply: one short, friendly line. If it is a greeting, mention that they can send anything to remember or ask about what they saved. No emoji.

Output exactly one JSON object, no prose, no markdown fences:
{"intent": "log"}
OR
{"intent": "query"}
OR
{"intent": "chat", "reply": "<one short line>"}"""

FANOUT_SYSTEM = """You are the inbox processor for a personal memory system. For each captured message you produce a structured fan-out PLAN: which records to create, of which type, with which field values. Code executes the plan against the database and validates every value.

# Hard rules (NEVER violate)
{hard_rules}

# Routing rules
{routing_rules}

# Shared vocabularies (use these EXACT strings; never invent new values)
{vocabularies}

# Record types and their fields
{types}

# Child trackers (write to these whenever the trigger matches; on multi-match write to ALL matching)
{children}

# Output format
Output ONLY a JSON object with this shape, no prose, no markdown fences:

{{
  "writes": [
    {{
      "type": "<type key from the list above, e.g. task, person, journal>",
      "title": "<the record's title (for people: the person's name)>",
      "fields": {{"<Field Name>": <value>, ...}},
      "rationale": "<one short sentence: why this write>"
    }}
  ],
  "updates": [
    {{
      "id": "<ID of an existing record from the Existing records lists>",
      "fields": {{"<Field Name>": <new value>, ...}},
      "rationale": "<one short sentence>"
    }}
  ],
  "processor_notes": "<one or two sentences summarising what you created or changed>"
}}

# Updates vs new records
- If the message reports progress on something that already exists, UPDATE it instead of creating a new record: a task done ("spoke to Meera already", "sent it") → Status "done" and Completed At = the capture date; a task dropped → Status "dropped"; rescheduled → new Due Date; blocked on someone → Status "waiting".
- Never create a task that duplicates an open task; update the open one.
- A message can do both: e.g. close one task and create a follow-up.
- Only update records listed under Existing records, by their ID.

# Field value formats
- text / url / email: a string
- number: a number
- date: ISO-8601 ("2026-05-25" or "2026-05-25T14:30:00+05:30")
- select: one option name, exactly as listed
- multi_select: a list of option names
- checkbox: true or false
- relation: a list of references. A reference is either the ID of an existing record from the "Existing records" lists in the message, or the exact title of a record you are creating in this same plan. If you cannot reference it confidently, leave the relation out.

# Discipline
- Do NOT set the body of Thoughts or Journal entries: code copies the verbatim message into them.
- Never write to types not listed above. Never set human-only fields of child trackers.
- People: one write per person mentioned. If the person already exists (see Existing records), still write them with the same name; code updates the existing record instead of duplicating it.
- Best effort: omit any optional field you can't fill confidently.
- Empty plan is valid: for pure noise output {{"writes": [], "updates": [], "processor_notes": "no actionable content"}}."""

FANOUT_USER = """Captured at: {captured_local}
Resolve every relative date in the message ("today", "tomorrow", "by Friday", "next week") against this local capture time. "By <weekday>" = the next such weekday on or after the capture date. Write dates as YYYY-MM-DD in this timezone.

Existing records (reference or update them by ID):
{context}

Message:
\"\"\"
{raw_content}
\"\"\"

Produce the fan-out plan JSON now."""

PLAN_SYSTEM = """You decide which record types to search, and with which keywords, to answer a personal question from the owner of a personal memory system.

# Record types
{types}

# Derived lenses (computed at query time — never stored)
{lenses}

# Output format
Output exactly one JSON object, no prose, no markdown fences:
{{
  "types": ["<type key>", ...],      // 1 to {max_types} entries
  "search": "<keywords for full-text search: names, topics, nouns from the question; empty string if the question is about recency or status rather than a topic>",
  "rationale": "<one short sentence>"
}}

Picking rules:
- Smallest set of types that lets you answer (task = actionable items, thought = atomic reflections, journal = day narratives, decision = commitments, person = contacts, library = things to read/watch/listen).
- For a derived lens (Discipline, Social health), list every type its definition references.
- Questions about a project usually need task + project."""

ANSWER_SYSTEM = """You answer questions from the owner of a personal memory system.

You may ONLY use the records provided in the user message. Do not guess, do not invent figures, do not refer to data you weren't shown.

# Hard rules
{hard_rules}

# Shared vocabularies (use these exact strings when referring to statuses, areas, etc.)
{vocabularies}

# Derived lenses (follow these definitions when asked about them)
{lenses}

# Style
- Start directly with the answer. No preamble, and never narrate your process ("Retrieve:", "Looking at the records...").
- Concise and declarative. Telegram-friendly short lines. Aim for under 250 words.
- For long lists, summarise first (counts, what is next, what is blocked), then give only the items that matter for the question. Offer to list the rest.
- If nothing relevant was found, say so in one or two sentences.
- Plain text only. No Markdown at all: no **bold**, no `backticks`, no # headers. Lists as plain "1." lines.
- Dates are given with their weekday; copy the weekday as given, never work one out yourself.
- Refer to records by their ID in brackets, e.g. [TSK-12], so the owner can find them.
- Now is {now}. Use it for anything relative ("last week", "overdue", "this Friday").
- If the records are empty or insufficient, say so plainly and say what would need to be captured."""


# --- implementation ------------------------------------------------------------

class ClaudeLLM:
    def __init__(self, api_key: str) -> None:
        self.client = Anthropic(api_key=api_key)

    def _call(self, *, model: str, system: str, user: str, max_tokens: int, cache: bool = True) -> str:
        sys_block: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache:
            sys_block[0]["cache_control"] = {"type": "ephemeral"}
        resp = self.client.messages.create(
            model=model, max_tokens=max_tokens, system=sys_block,
            messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

    def classify(self, text: str) -> dict:
        raw = self._call(model=CLASSIFIER_MODEL, system=CLASSIFY_SYSTEM,
                         user=text or "(empty)", max_tokens=120, cache=False)
        obj = parse_json(raw)
        intent = str(obj.get("intent", "")).lower().strip()
        if intent not in ("log", "query", "chat"):
            raise LLMError(f"unexpected intent {intent!r}")
        reply = str(obj.get("reply") or "").strip()
        if intent == "chat" and not reply:
            reply = "Hi. Send me anything you want remembered, or ask me about what you've saved."
        return {"intent": intent, "reply": reply}

    def plan_fanout(self, *, schema: Schema, raw_content: str, captured_local: str,
                    context: dict[str, list[dict]]) -> dict:
        system = FANOUT_SYSTEM.format(
            hard_rules=schema.rules_hard, routing_rules=schema.rules_routing,
            vocabularies=schema.render_vocabularies(), types=schema.render_types(),
            children=schema.render_children())
        user = FANOUT_USER.format(captured_local=captured_local, context=render_context(context),
                                  raw_content=raw_content[:8000])
        plan = parse_json(self._call(model=MODEL, system=system, user=user, max_tokens=2048))
        if not isinstance(plan.get("writes", []), list):
            raise LLMError("plan.writes is not a list")
        plan.setdefault("writes", [])
        plan.setdefault("updates", [])
        plan.setdefault("processor_notes", "")
        if not isinstance(plan["updates"], list):
            raise LLMError("plan.updates is not a list")
        return plan

    def plan_query(self, *, schema: Schema, question: str, now: str) -> dict:
        system = PLAN_SYSTEM.format(types=schema.render_types(), lenses=schema.rules_lenses,
                                    max_types=MAX_TYPES_TO_SEARCH)
        plan = parse_json(self._call(model=MODEL, system=system,
                                     user=f"Now: {now}\nQuestion: {question}", max_tokens=300))
        types = [t for t in plan.get("types", []) if isinstance(t, str)][:MAX_TYPES_TO_SEARCH]
        return {"types": types, "search": str(plan.get("search", "")),
                "rationale": str(plan.get("rationale", ""))}

    def answer(self, *, schema: Schema, question: str, records: list[dict], now: str) -> str:
        system = ANSWER_SYSTEM.format(hard_rules=schema.rules_hard,
                                      vocabularies=schema.render_vocabularies(),
                                      lenses=schema.rules_lenses, now=now)
        data = json.dumps(records, ensure_ascii=False, default=str)
        if len(data) > 60000:
            data = data[:60000] + " ...[truncated]"
        user = f"Question:\n{question}\n\nRecords:\n{data if records else '(none found)'}"
        return self._call(model=MODEL, system=system, user=user, max_tokens=2048) or "(no answer)"


def render_context(context: dict[str, list[dict]]) -> str:
    lines: list[str] = []
    for label, rows in context.items():
        if not rows:
            continue
        lines.append(f"{label}:")
        lines += [f"- {r['id']}  {r['title']}" + (f"  ({r['note']})" if r.get("note") else "")
                  for r in rows]
    return "\n".join(lines) or "(none)"


def parse_json(raw: str) -> dict:
    """Take the first JSON object in the response, tolerating fences and trailing prose."""
    raw = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip(), flags=re.IGNORECASE)
    start = raw.find("{")
    if start == -1:
        raise LLMError(f"no JSON object in response: {raw[:400]!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as e:
        raise LLMError(f"non-JSON response: {raw[:400]!r}") from e
    if not isinstance(obj, dict):
        raise LLMError("response JSON is not an object")
    return obj
