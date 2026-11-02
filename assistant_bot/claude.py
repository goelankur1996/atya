"""Anthropic wrappers — one file, four functions.

System prompts live here as module-level constants; they're load-bearing for
the agent's behavior and worth keeping under version control.
"""

from __future__ import annotations

import json
import re

from anthropic import Anthropic

from .schema import Schema, db_property_summary

CLASSIFIER_MODEL = "claude-haiku-4-5"  # cheap binary log|query decision
MODEL = "claude-sonnet-4-6"            # fanout planning + query planning/answering
MAX_DBS_TO_QUERY = 4


class LLMError(Exception):
    pass


# --- Classifier ----------------------------------------------------------

CLASSIFY_SYSTEM = """You classify a single incoming message as either a "log" or a "query".

- log = a capture: a declarative statement, observation, event, link, person mention, commitment, decision, journal entry, reading material, training log, meal log, etc.
- query = a question or request for information ("what / who / when / where / why / how / can / should / is / are / show me / list / how many", or any other request to retrieve known data).

Tie-breaker: if the message is genuinely ambiguous, output "query" — answering is safer than a spurious write to canonical stores.

Output exactly one JSON object, no prose, no markdown fences:
{"intent": "log"}
OR
{"intent": "query"}"""


def classify_intent(text: str, client: Anthropic) -> str:
    resp = client.messages.create(
        model=CLASSIFIER_MODEL, max_tokens=20, system=CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": text or "(empty)"}],
    )
    raw = _text(resp)
    obj = _parse_json(raw)
    intent = obj.get("intent")
    if not isinstance(intent, str):
        raise LLMError(f"missing/invalid intent in {obj!r}")
    intent = intent.lower().strip()
    if intent not in ("log", "query"):
        raise LLMError(f"unexpected intent {intent!r}")
    return intent


# --- Fanout (LOG path) ---------------------------------------------------

FANOUT_SYSTEM = """You are the Inbox processor for a Personal OS built on Notion. For each captured message, you produce a structured fan-out PLAN: which databases to write rows to, and what each row's properties should be. Another piece of code executes the plan against the Notion API.

# FORBIDDEN targets (never include in your plan, ever — case does not matter)
You MUST NOT produce any "writes" entry with database equal to (case-insensitive): "Inbox", "Outbox", or "Audit Log". The agent manages those itself. Plans naming any of them will be hard-rejected. Inbox is verbatim raw capture and must never be mutated or duplicated by AI.

# Hard rules (NEVER violate)
{hard_rules}

# Routing rules — Inbox processor fan-out
{routing_rules}

# Shared vocabularies (use these EXACT strings; never invent new select values)
{vocabularies}

# Available databases (with their property schema)
{db_schemas}

# Child databases (write to these whenever the trigger matches; write to ALL matching children on multi-match)
{child_registry}

# Output format
Output ONLY a JSON object with this shape, no prose, no markdown fences:

{{
  "writes": [
    {{
      "database": "<exact DB name as it appears in the registry above>",
      "properties": {{
        "<PropertyName>": <value>,
        ...
      }},
      "body_text": "<optional verbatim text to put in the page body (REQUIRED for Journal entries — Hard Rule 0a)>",
      "rationale": "<one short sentence: why this write>"
    }},
    ...
  ],
  "processor_notes": "<one or two sentences summarizing what you created, for the Inbox row's Processor Notes field>"
}}

# Property value formats
For each Notion property type, use the value form shown:
- title:        a plain string, max ~200 chars
- text / rich_text: a plain string (will be split if >2000 chars)
- select / status: the option name as a string (must match an existing option exactly)
- date:         an ISO-8601 string (e.g. "2026-05-25" or "2026-05-25T14:30:00Z")
- url:          a URL string
- number:       a numeric value
- multi_select: an array of option name strings
- relation:     an array of Notion page IDs (UUIDs). DO NOT use names; if you don't know an ID, skip the property.
- people:       SKIP — never set (no user account info available)
- auto_increment_id / created_time / created_by / last_edited_*: SKIP — system-managed

# Discipline
- Verbatim rule: for Journal entries, the page body MUST be the raw Inbox text exactly — set "body_text" to the original Raw Content. Do not summarize.
- Source field: every created row's "Source" property must be set to the Inbox page ID (passed below). This is how idempotency works.
- Best-effort: if you can't fill an optional field confidently, omit it (leave blank for human edit).
- Selects: validate against the shared vocabularies and the schema's options list. Never invent new options.
- Empty plan is valid: if the message is pure noise (e.g. an empty caption) output {{"writes": [], "processor_notes": "no actionable content"}}.
- Child DBs: for every child whose trigger matches the content, include a write to it. Multi-match → write to ALL matching."""

FANOUT_USER = """Inbox row:
- page_id (use as Source): {inbox_id}
- captured_at: {captured_at}
- sender: {sender}

Raw Content:
\"\"\"
{raw_content}
\"\"\"

Produce the fan-out plan JSON now."""

EXCLUDED_FROM_FANOUT_PROMPT = frozenset({"Inbox", "Outbox", "Audit Log"})


def plan_fanout(
    *,
    raw_content: str,
    sender: str,
    captured_at: str,
    inbox_id: str,
    schema: Schema,
    db_schemas: dict[str, dict],
    child_db_schemas: dict[str, dict],
    client: Anthropic,
) -> dict:
    db_summaries: list[str] = []
    for name, ds_id in schema.database_registry.items():
        if name in EXCLUDED_FROM_FANOUT_PROMPT:
            continue
        s = db_schemas.get(name)
        if not s:
            continue
        db_summaries.append(f"## {name} (data_source_id={ds_id})\n" + db_property_summary(s))

    child_summaries: list[str] = []
    for child in schema.child_registry:
        s = child_db_schemas.get(child.data_source_id)
        props = db_property_summary(s) if s else "(schema not loaded)"
        child_summaries.append(
            f"## {child.name} (data_source_id={child.data_source_id})\n"
            f"  parent_project: {child.parent_project}\n"
            f"  trigger: {child.trigger}\n"
            f"  fillable: {child.fillable_fields}\n"
            f"  properties:\n{props}"
        )

    vocab = "\n\n".join(f"### {n}\n{t}" for n, t in schema.vocabularies.items()) or "(none)"
    system_text = FANOUT_SYSTEM.format(
        hard_rules=schema.hard_rules_text or "(missing)",
        routing_rules=schema.routing_rules_text or "(missing)",
        vocabularies=vocab,
        db_schemas="\n\n".join(db_summaries) or "(none)",
        child_registry="\n\n".join(child_summaries) or "(none)",
    )
    user_text = FANOUT_USER.format(
        inbox_id=inbox_id, captured_at=captured_at, sender=sender,
        raw_content=raw_content[:8000],
    )

    resp = client.messages.create(
        model=MODEL, max_tokens=2048,
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_text}],
    )
    plan = _parse_json(_text(resp))
    if not isinstance(plan, dict):
        raise LLMError(f"plan not a JSON object: {plan!r}")
    plan.setdefault("writes", [])
    plan.setdefault("processor_notes", "")
    if not isinstance(plan["writes"], list):
        raise LLMError("plan.writes is not a list")
    return plan


# --- Query path: planner + answerer --------------------------------------

PLAN_SYSTEM = """You decide which Notion databases to read to answer a personal question for the owner of a Personal OS. The system's rules below come from the user's authoritative "System Schema" Notion page — they are not your guesses.

# Hard rules (NEVER violate)
{hard_rules}

# Available databases (with property listings)
{db_summaries}

# Shared vocabularies (use these EXACT strings if you need filter hints — never invent new ones)
{vocabularies}

# Derived lenses (computed from data at query time — never stored)
{derived_lenses}

# Cross-database relations (for context, you cannot follow these here)
{cross_relations}

# Output format
Output exactly one JSON object, no prose, no markdown fences:
{{
  "databases": ["<DB Name from registry>", ...],   // 1 to {max_dbs} entries
  "rationale": "<one short sentence on why these>"
}}

Picking rules:
- Smallest set of databases that lets you answer. Be precise about which DBs hold which entity types (Tasks=actionable items, Thoughts=atomic reflections, Journal=day narratives, Decisions=commitments, People=contacts, Reading Queue=links/articles).
- If the question asks for a Derived Lens (Discipline, Social health, …), list ALL of the databases the lens definition above references. Do not improvise a different computation.
- If the question references a Project, Area, or other vocab value, the DB choice should match — e.g. "what's blocking the Pullup goal" → Tasks + Projects."""

ANSWER_SYSTEM = """You answer questions from the owner of a Personal OS built on Notion. The rules below come from the user's authoritative "System Schema" Notion page — follow them literally.

You may ONLY use data provided in the user message below (the retrieved Notion rows). Do not guess, do not invent figures, do not refer to data you weren't shown.

# Hard rules (NEVER violate)
{hard_rules}

# Shared vocabularies (when you reference a Status/Area/Priority/etc., use these EXACT strings)
{vocabularies}

# Derived lenses (computed definitions you MUST follow when asked about these)
{derived_lenses}

# Style
- Concise, declarative, no preamble.
- Telegram-friendly: short lines, no fluff, no markdown headers.
- If the retrieved data is empty or insufficient, say so explicitly and suggest what would need to be captured.
- If the question is asking for a Derived Lens, compute it per the definition above — report the signals, not a single fabricated score.
- Reference exact data points (titles, dates, counts) over impressions."""


def plan_query_dbs(*, question: str, schema: Schema, db_schemas: dict[str, dict],
                   client: Anthropic) -> dict:
    db_summaries: list[str] = []
    for name, ds_id in schema.database_registry.items():
        s = db_schemas.get(name)
        if not s:
            db_summaries.append(f"## {name}\n  (schema not loaded)")
            continue
        db_summaries.append(f"## {name}\n" + db_property_summary(s))

    vocab = "\n\n".join(f"### {n}\n{t}" for n, t in schema.vocabularies.items()) or "(none)"
    system_text = PLAN_SYSTEM.format(
        hard_rules=schema.hard_rules_text or "(none)",
        db_summaries="\n\n".join(db_summaries),
        vocabularies=vocab,
        derived_lenses=schema.derived_lenses_text or "(none)",
        cross_relations=schema.cross_relations_text or "(none)",
        max_dbs=MAX_DBS_TO_QUERY,
    )
    resp = client.messages.create(
        model=MODEL, max_tokens=256,
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": question}],
    )
    plan = _parse_json(_text(resp))
    if not isinstance(plan, dict) or not isinstance(plan.get("databases"), list):
        raise LLMError(f"plan shape invalid: {plan!r}")
    plan["databases"] = plan["databases"][:MAX_DBS_TO_QUERY]
    return plan


def answer_query(*, question: str, retrieved: list[dict], schema: Schema,
                 client: Anthropic) -> str:
    blocks: list[str] = []
    for r in retrieved:
        rows_json = json.dumps(r["rows"], default=str, ensure_ascii=False, indent=2)
        if len(rows_json) > 30000:
            rows_json = rows_json[:30000] + "\n... [truncated]"
        blocks.append(f"# Database: {r['database']}\n{rows_json}")
    if blocks:
        user_text = f"Question:\n{question}\n\nRetrieved data:\n\n" + "\n\n".join(blocks)
    else:
        user_text = f"Question:\n{question}\n\nRetrieved data: (none — no databases were queried)"

    vocab = "\n\n".join(f"### {n}\n{t}" for n, t in schema.vocabularies.items()) or "(none)"
    system_text = ANSWER_SYSTEM.format(
        hard_rules=schema.hard_rules_text or "(none)",
        vocabularies=vocab,
        derived_lenses=schema.derived_lenses_text or "(none)",
    )
    resp = client.messages.create(
        model=MODEL, max_tokens=1024,
        system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_text}],
    )
    return _text(resp) or "(no answer)"


# --- helpers -------------------------------------------------------------

def _text(resp) -> str:
    return "".join(
        b.text for b in resp.content if getattr(b, "type", None) == "text"
    ).strip()


def _parse_json(raw: str) -> dict:
    # Models sometimes wrap the JSON in ```json fences or follow it with prose
    # despite instructions. Take the first valid JSON object and ignore the rest.
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    start = raw.find("{")
    if start == -1:
        raise LLMError(f"no JSON object in response: {raw[:400]!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as e:
        raise LLMError(f"non-JSON response: {raw[:400]!r}") from e
    return obj
