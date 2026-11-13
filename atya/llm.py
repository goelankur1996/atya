"""Model calls: classify, plan the fan-out, plan the search, answer, read files.

Prompts are ported from v1 (assistant_bot/claude.py) with Notion wording
replaced. The `LLM` protocol lets tests swap in a fake.

`PromptLLM` holds the prompts and parsing; a transport sends them. Two
transports: `ClaudeLLM` (Anthropic API) and `BedrockLLM` (any model on Amazon
Bedrock, through the Converse API). Each uses two models: a fast one
(classify, broaden, read files) and a main one (plan, answer).
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
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
                    context: dict[str, list[dict]], hint: str | None = None) -> dict: ...
    def plan_query(self, *, schema: Schema, question: str, now: str) -> dict: ...
    def broaden_search(self, *, question: str, tried: str, found: list[str]) -> str: ...
    def answer(self, *, schema: Schema, question: str, records: list[dict], now: str) -> dict: ...
    def interview_next(self, *, transcript: list[dict], areas: dict[str, str], still_open: list[str],
                       saved: int, target: int) -> dict: ...
    def research_plan(self, *, request: str, answers: str | None, allow_questions: bool,
                      timezone: str, today: str) -> dict: ...
    def research_write(self, *, request: str, goal: str, findings: list[dict]) -> dict: ...
    def read_document(self, *, filename: str, caption: str, text: str | None = None,
                      pdf: bytes | None = None) -> dict: ...


# --- prompts -------------------------------------------------------------------

CLASSIFY_SYSTEM = """You classify a single message sent to a personal memory bot as "log", "query", "research" or "chat".

- log = a capture: a declarative statement, observation, event, link, person mention, commitment, decision, journal entry, reading material, training log, meal log, etc.
- query = a question or request for information ("what / who / when / where / why / how / can / should / is / are / show me / list / how many", or any other request to retrieve known data).
- research = the owner asks the bot itself to find something out on the web now: "research X", "find me 3 good physios in Noida", "look up the visa rules for Japan for me", "compare the Kindle and the Kobo", "what are the best budget hotels in Goa?". Only when the answer has to come from the internet, not from what they saved, and they want it now.
- chat = pure small talk addressed to the bot with nothing to save and nothing to look up: greetings, thanks, "ok", "are you there?", "test".

Instructions to self are captures, not questions. A message that tells the owner (or the bot) to do something later is "log", because it should become a task: "check on the Costa Rica product", "follow up with Meera", "remind me to renew the passport", "need to call the bank", "buy batteries", "book flights to Goa", "look into Voyage pricing". Only a message that asks for information already saved is "query": "what's the status of the Costa Rica product?", "did I follow up with Meera?", "check what I owe Rahul" (asks to look something up).

Tie-breakers: a message with any real information in it is "log", even if it starts with a greeting ("hi, met Rahul today" = log). An imperative about a future action is "log": "look into Voyage pricing" is a note to self (log); "look up Voyage pricing for me" asks the bot (research). If it is still genuinely ambiguous between log and query, output "query".

For chat only, also write a reply: one short, friendly line. If it is a greeting, mention that they can send anything to remember or ask about what they saved. No emoji.

Output exactly one JSON object, no prose, no markdown fences:
{"intent": "log"}
OR
{"intent": "query"}
OR
{"intent": "research"}
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
  "search": "<keywords for full-text search; empty string if the question is about recency or status rather than a topic>",
  "rationale": "<one short sentence>"
}}

Picking rules:
- Smallest set of types that lets you answer (task = actionable items, thought = atomic reflections, journal = day narratives, decision = commitments, person = contacts, library = things to read/watch/listen).
- For a derived lens (Discipline, Social health), list every type its definition references.
- Questions about a project usually need task + project.

Keyword rules (the search matches whole words, any one word is enough, and it does not understand meaning):
- Include the names, topics and nouns from the question.
- Also add the other words the owner might have used when writing it down: synonyms, related terms, abbreviations and their expansions, alternative spellings ("gym workout exercise training lift" for a question about the gym; "VLA vision-language-action policy model" for VLA).
- 4 to 15 words. No stop words, no dates."""

BROADEN_SYSTEM = """A keyword search over someone's personal notes found too little to answer their question. Suggest other ways the SAME things might have been written, so the search can find notes that use different wording.

The search matches whole words (any one word is enough) and does not understand meaning.

Allowed: synonyms and near-synonyms of the question's own words, singular/plural and other word forms, abbreviations and their expansions, alternative spellings, and a person's or place's other names ("Costa Rica" -> "CR costarica"; "gym" -> "workout exercise training"; "pricing" -> "price prices cost quote").
Not allowed: guessing what the subject might relate to. Never add topics, products, industries, people or places the question doesn't name (for "Costa Rica product", do NOT add coffee, banana, export, supplier). A wrong guess finds unrelated notes, which is worse than finding nothing.

Do not repeat words already tried. If there is nothing sensible to add, return an empty string.

Output exactly one JSON object, no prose, no markdown fences:
{"search": "<0 to 10 words, space-separated>"}"""

READ_DOCUMENT_SYSTEM = """You read a document (a PDF) that the owner of a personal memory system just saved, so it can be filed and found later.

Write for the owner: plain, factual, no filler. Use only what the document says; if something isn't stated (a date, an author), leave it empty rather than guessing.

Output exactly one JSON object, no prose, no markdown fences:
{
  "title": "<what the document is, e.g. 'VVPM services agreement' or 'Invoice 2213 from AWS'; not the file name unless nothing better>",
  "kind": "<one of: contract, invoice, receipt, report, paper, statement, letter, presentation, form, other>",
  "date": "<the document's own date as YYYY-MM-DD, or empty>",
  "from": "<who issued, sent or wrote it, or empty>",
  "summary": "<2-3 sentences: what it is and what matters in it>",
  "key_points": ["<up to 8 short lines: amounts, parties, terms, deadlines, findings>"],
  "actions": ["<things the document asks of the owner, with dates: 'Pay $1,240 by 2026-11-30'; empty if none>"]
}"""

READ_SCAN_EXTRA = """

This PDF is a scan (no text layer). After the JSON object, write a line containing only ===== and then the document's readable text, page by page, each page starting with [page N]. Transcribe faithfully; skip decorative elements; use [illegible] where needed."""

INTERVIEW_SYSTEM = """You are setting up a personal memory assistant for a new user by interviewing them, one question at a time. Everything they answer is filed as tasks, reminders, people, projects and goals, so the assistant is useful from day one, above all this week. The aim is many concrete, dated facts, not a pleasant chat.

Areas (key: what to find out):
{areas}

How to ask:
- One question per turn, short (under 30 words), warm and plain, like a sharp assistant on their first day.
- Go after concrete facts with dates: who, what, which day, what time. If something time-bound came without a date (a birthday, a trip, a deadline, a bill), ask for the date next.
- Follow up on specifics they just gave when something useful is missing: a person with no context, a task with no next step. Use their own words and names. Two follow-ups on one thread at most, then move on.
- Then move to an area still open (listed in the message), in the order given, phrased naturally.
- Short answers are fine; never wrap up because answers are short. If an area gets "nothing" or a skip, it is covered.
- Ask in the language they answer in.

Reply with JSON only:
{{"covered": ["<keys of all areas the conversation has now covered or the user said have nothing>"], "question": "<the next question>", "done": <true only if every area is covered and there is nothing useful left to ask>, "suggest": "<one question they could ask the assistant later about their own answers, e.g. What do I owe Rahul?>"}}"""

RESEARCH_PLAN_SYSTEM = """You plan a web research task for one person, who asked for it in a chat app.

First decide whether the request is specific enough to research well. If a good answer depends on things only they know and didn't say (budget, area or city, dates, who it's for, must-haves, what they already tried), ask for them: up to 3 short questions, in one message. Don't ask when a sensible default is obvious (their timezone tells you their country and likely city), or when the request is already specific, or when they've already answered your questions (then plan with what you have; never ask twice).

Then plan the searches: 2 to 4 web search queries that together cover the request from different angles (options, prices, reviews or comparisons, recent changes or rules). Specific queries beat broad ones.

The person's timezone: {timezone}. Today: {today}.

Reply with JSON only:
{{"ask": ["<question>", ...] or [], "queries": ["<search query>", ...], "goal": "<one line: what a great answer must contain>"}}"""

RESEARCH_WRITE_SYSTEM = """You write up web research for one person, from findings gathered by several searches (each with its sources). Use only what the findings say; when they disagree or something wasn't found, say so plainly. Don't invent prices, names, numbers or links.

Write two things:
- "reply": for a chat message, under 220 words. Lead with the answer or recommendation. For options, 3 to 5, each in one line with the specifics that matter (price, place, why it fits). End with one line on what to check or do next if useful. Plain text, "- " bullets, no headings, no tables.
- "report": the fuller write-up to keep, under 700 words: answer, options with details, trade-offs, caveats, and what's uncertain. Plain text, "- " bullets allowed.

Reply with JSON only:
{{"reply": "...", "report": "..."}}"""

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
- Relevance first: decide whether each record is actually about what was asked. Records are retrieved by loose keyword matching, plus some recent records for context, so many will be unrelated. Use only the relevant ones. Never present an unrelated record as the answer, and never answer a different question than the one asked.
- If nothing relevant was found, say so plainly in one or two sentences, naming the subject ("I don't have anything about the Costa Rica product yet."). Do not list unrelated records instead.
- Plain text only. No Markdown at all: no **bold**, no `backticks`, no # headers. Lists as plain "1." lines.
- Dates are given with their weekday; copy the weekday as given, never work one out yourself.
- Refer to records by their ID in brackets, e.g. [TSK-12], so the owner can find them.
- Some items are excerpts from the owner's own files (PDFs), marked "document_excerpt". Use them for facts in those documents and cite the document and page, e.g. "[DOC-3] p.4". Only use an excerpt if it actually answers the question.
- Now is {now}. Use it for anything relative ("last week", "overdue", "this Friday").
- If the records are empty or insufficient, say so plainly and say what would need to be captured.

# Output
Exactly one JSON object, no prose around it, no markdown fences:
{{"found": true or false, "answer": "<the reply text, plain text as described above>"}}
"found" is false when none of the records is relevant to the question (so the reply is essentially "I don't have anything about X"), true otherwise."""


# --- implementation ------------------------------------------------------------

class PromptLLM:
    """The prompts. Subclasses implement _call and _call_pdf."""

    fast_model: str
    main_model: str

    def __init__(self) -> None:
        # Per-model usage, for the evaluation report: {model: {"calls", "in", "out", "secs"}}.
        self.usage: dict[str, dict[str, float]] = {}
        self._usage_lock = threading.Lock()

    def _note(self, model: str, tokens_in: int, tokens_out: int, secs: float) -> None:
        with self._usage_lock:
            self._note_locked(model, tokens_in, tokens_out, secs)

    def _note_locked(self, model: str, tokens_in: int, tokens_out: int, secs: float) -> None:
        u = self.usage.setdefault(model, {"calls": 0, "in": 0, "out": 0, "secs": 0.0})
        u["calls"] += 1
        u["in"] += tokens_in
        u["out"] += tokens_out
        u["secs"] += secs

    def _call(self, *, model: str, system: str, user: str, max_tokens: int, cache: bool = True) -> str:
        raise NotImplementedError

    def _call_pdf(self, *, model: str, system: str, intro: str, pdf: bytes, max_tokens: int) -> str:
        raise NotImplementedError

    def classify(self, text: str) -> dict:
        raw = self._call(model=self.fast_model, system=CLASSIFY_SYSTEM,
                         user=text or "(empty)", max_tokens=120, cache=False)
        obj = parse_json(raw)
        intent = str(obj.get("intent", "")).lower().strip()
        if intent not in ("log", "query", "chat", "research"):
            raise LLMError(f"unexpected intent {intent!r}")
        reply = str(obj.get("reply") or "").strip()
        if intent == "chat" and not reply:
            reply = "Hi. Send me anything you want remembered, or ask me about what you've saved."
        return {"intent": intent, "reply": reply}

    def plan_fanout(self, *, schema: Schema, raw_content: str, captured_local: str,
                    context: dict[str, list[dict]], hint: str | None = None) -> dict:
        system = FANOUT_SYSTEM.format(
            hard_rules=schema.rules_hard, routing_rules=schema.rules_routing,
            vocabularies=schema.render_vocabularies(), types=schema.render_types(),
            children=schema.render_children())
        user = FANOUT_USER.format(captured_local=captured_local, context=render_context(context),
                                  raw_content=raw_content[:8000])
        if hint:
            user += f"\n\nNote from the owner: {hint}"
        plan = parse_json(self._call(model=self.main_model, system=system, user=user, max_tokens=2048))
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
        plan = parse_json(self._call(model=self.main_model, system=system,
                                     user=f"Now: {now}\nQuestion: {question}", max_tokens=300))
        types = [t for t in plan.get("types", []) if isinstance(t, str)][:MAX_TYPES_TO_SEARCH]
        return {"types": types, "search": str(plan.get("search", "")),
                "rationale": str(plan.get("rationale", ""))}

    def broaden_search(self, *, question: str, tried: str, found: list[str]) -> str:
        """Second attempt when the first keyword search came back thin."""
        user = (f"Question: {question}\nWords already tried: {tried}\n"
                f"Titles found so far: {'; '.join(found[:10]) if found else '(nothing)'}")
        obj = parse_json(self._call(model=self.fast_model, system=BROADEN_SYSTEM, user=user,
                                    max_tokens=150, cache=False))
        return str(obj.get("search", "")).strip()

    def answer(self, *, schema: Schema, question: str, records: list[dict], now: str) -> str:
        system = ANSWER_SYSTEM.format(hard_rules=schema.rules_hard,
                                      vocabularies=schema.render_vocabularies(),
                                      lenses=schema.rules_lenses, now=now)
        data = json.dumps(records, ensure_ascii=False, default=str)
        if len(data) > 60000:
            data = data[:60000] + " ...[truncated]"
        user = f"Question:\n{question}\n\nRecords:\n{data if records else '(none found)'}"
        raw = self._call(model=self.main_model, system=system, user=user, max_tokens=2048)
        return parse_answer(raw, had_records=bool(records))

    def interview_next(self, *, transcript: list[dict], areas: dict[str, str], still_open: list[str],
                       saved: int, target: int) -> dict:
        """The next onboarding question, from the conversation so far."""
        convo = "\n\n".join(f"Q: {t['q']}\nA: {t['a'] if t.get('a') else '(skipped)'}" for t in transcript)
        status = (f"\n\n---\nAreas still open, in order: {', '.join(still_open) or '(none)'}\n"
                  f"Things saved so far: {saved} (aim: at least {target}).")
        system = INTERVIEW_SYSTEM.format(areas="\n".join(f"- {k}: {v}" for k, v in areas.items()))
        obj = parse_json(self._call(model=self.main_model, system=system,
                                    user=(convo[-12000:] or "(no answers yet)") + status, max_tokens=400, cache=False))
        q = str(obj.get("question") or "").strip()
        covered = [str(c) for c in (obj.get("covered") or []) if str(c) in areas]
        return {"done": bool(obj.get("done")) or not q, "question": q, "covered": covered,
                "suggest": str(obj.get("suggest") or "").strip()}

    def research_plan(self, *, request: str, answers: str | None, allow_questions: bool,
                      timezone: str, today: str) -> dict:
        """Clarifying questions (if needed and allowed) and the searches to run."""
        user = f"Request: {request}"
        if answers is not None:
            user += f"\n\nTheir answers to your questions: {answers or '(they said to just go ahead)'}"
        if not allow_questions:
            user += "\n\nDo not ask anything; plan the searches."
        obj = parse_json(self._call(model=self.main_model,
                                    system=RESEARCH_PLAN_SYSTEM.format(timezone=timezone, today=today),
                                    user=user, max_tokens=500, cache=False))
        ask = [str(q).strip() for q in (obj.get("ask") or []) if str(q).strip()][:3] if allow_questions else []
        queries = [str(q).strip() for q in (obj.get("queries") or []) if str(q).strip()][:4]
        return {"ask": ask, "queries": queries or [request], "goal": str(obj.get("goal") or "").strip()}

    def research_write(self, *, request: str, goal: str, findings: list[dict]) -> dict:
        """Turn the findings of several searches into a chat reply and a report to keep."""
        body = "\n\n".join(f"Search {i}: {f['query']}\nFindings:\n{f['text']}\nSources: {', '.join(f['sources'][:6]) or '(none)'}"
                            for i, f in enumerate(findings, 1))
        obj = parse_json(self._call(model=self.main_model, system=RESEARCH_WRITE_SYSTEM,
                                    user=f"Request: {request}\nA great answer must: {goal or '(answer it well)'}\n\n{body[:40000]}",
                                    max_tokens=2500, cache=False))
        reply = str(obj.get("reply") or "").strip()
        if not reply:
            raise LLMError("research write-up came back empty")
        return {"reply": reply, "report": str(obj.get("report") or reply).strip()}

    def read_document(self, *, filename: str, caption: str, text: str | None = None,
                      pdf: bytes | None = None) -> dict:
        """Summarise a PDF. With text: read the extracted text (cheap). With pdf
        bytes (a scan): the model reads the pages and also transcribes them."""
        intro = f"File name: {filename}\nThe owner's message with it: {caption or '(none)'}\n"
        if text is not None:
            raw = self._call(model=self.fast_model, system=READ_DOCUMENT_SYSTEM,
                             user=intro + "\nDocument text:\n" + text[:120_000], max_tokens=1500, cache=False)
        else:
            assert pdf is not None
            raw = self._call_pdf(model=self.fast_model, system=READ_DOCUMENT_SYSTEM + READ_SCAN_EXTRA,
                                 intro=intro, pdf=pdf, max_tokens=16000)
        return parse_reading(raw)


class ClaudeLLM(PromptLLM):
    def __init__(self, api_key: str, *, fast_model: str = CLASSIFIER_MODEL, main_model: str = MODEL) -> None:
        super().__init__()
        self.client = Anthropic(api_key=api_key)
        self.fast_model, self.main_model = fast_model, main_model

    def _send(self, *, model: str, system: Any, content: Any, max_tokens: int) -> str:
        t0 = time.monotonic()
        resp = self.client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                           messages=[{"role": "user", "content": content}])
        u = getattr(resp, "usage", None)
        self._note(model, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0,
                   time.monotonic() - t0)
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()

    def _call(self, *, model: str, system: str, user: str, max_tokens: int, cache: bool = True) -> str:
        sys_block: list[dict[str, Any]] = [{"type": "text", "text": system}]
        if cache:
            sys_block[0]["cache_control"] = {"type": "ephemeral"}
        return self._send(model=model, system=sys_block, content=user, max_tokens=max_tokens)

    def _call_pdf(self, *, model: str, system: str, intro: str, pdf: bytes, max_tokens: int) -> str:
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.b64encode(pdf).decode()}},
            {"type": "text", "text": intro},
        ]
        return self._send(model=model, system=system, content=content, max_tokens=max_tokens)


THINK = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)


class BedrockLLM(PromptLLM):
    """Any Bedrock model through the Converse API, which is the same for all of
    them. Credentials come from the server's IAM role."""

    def __init__(self, *, fast_model: str, main_model: str, region: str = "us-east-1",
                 client: Any = None) -> None:
        super().__init__()
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client("bedrock-runtime", region_name=region,
                                  config=Config(read_timeout=300, retries={"max_attempts": 4, "mode": "adaptive"}))
        self.client = client
        self.fast_model, self.main_model = fast_model, main_model
        self._no_system: set[str] = set()      # models that reject a system prompt

    def _converse(self, *, model: str, system: str, content: list[dict], max_tokens: int) -> str:
        kwargs: dict[str, Any] = {"modelId": model, "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2}}
        if model in self._no_system:
            content = [{"text": system + "\n\n---\n\n"}] + content
        else:
            kwargs["system"] = [{"text": system}]
        t0 = time.monotonic()
        try:
            resp = self.client.converse(messages=[{"role": "user", "content": content}], **kwargs)
        except Exception as e:
            if "system" in str(e).lower() and model not in self._no_system and "ValidationException" in type(e).__name__ + str(e):
                self._no_system.add(model)
                return self._converse(model=model, system=system, content=content, max_tokens=max_tokens)
            raise LLMError(f"{model}: {e}") from e
        u = resp.get("usage") or {}
        self._note(model, u.get("inputTokens", 0), u.get("outputTokens", 0), time.monotonic() - t0)
        parts = (resp.get("output") or {}).get("message", {}).get("content", [])
        text = "".join(p["text"] for p in parts if isinstance(p, dict) and "text" in p)
        return THINK.sub("", text).strip()

    def _call(self, *, model: str, system: str, user: str, max_tokens: int, cache: bool = True) -> str:
        return self._converse(model=model, system=system, content=[{"text": user}], max_tokens=max_tokens)

    def _call_pdf(self, *, model: str, system: str, intro: str, pdf: bytes, max_tokens: int) -> str:
        doc = {"document": {"format": "pdf", "name": "document", "source": {"bytes": pdf}}}
        return self._converse(model=model, system=system, content=[doc, {"text": intro}], max_tokens=max_tokens)


def parse_reading(raw: str) -> dict:
    """The read_document JSON, plus the transcription after '=====' for scans."""
    head, _, tail = (raw or "").partition("\n=====")
    obj = parse_json(head)
    out = {
        "title": str(obj.get("title") or "").strip()[:200],
        "kind": str(obj.get("kind") or "").strip().lower(),
        "date": str(obj.get("date") or "").strip()[:10],
        "from": str(obj.get("from") or "").strip()[:200],
        "summary": str(obj.get("summary") or "").strip()[:1500],
        "key_points": [str(k).strip() for k in (obj.get("key_points") or []) if str(k).strip()][:8],
        "actions": [str(a).strip() for a in (obj.get("actions") or []) if str(a).strip()][:8],
        "text": tail.strip(" \n=") if tail else "",
    }
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", out["date"]):
        out["date"] = ""
    return out


def parse_answer(raw: str, *, had_records: bool) -> dict:
    """{"text", "found"} from the answer model's JSON. If the model ignored the
    format, use its text as the answer and assume something was found."""
    try:
        obj = parse_json(raw)
        text = str(obj.get("answer") or "").strip()
        if text:
            return {"text": text, "found": bool(obj.get("found", True)) and had_records}
    except Exception:
        pass
    return {"text": (raw or "").strip() or "(no answer)", "found": had_records}


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
