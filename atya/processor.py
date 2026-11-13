"""Worker 2: inbox -> classify -> records (log) or answer (query).

Flow per inbox row:
1. Claim it (its own short transaction; SKIP LOCKED).
2. Call the LLM outside any transaction (classify, then plan).
3. Write everything in ONE transaction: records, links, outbox, audit, and the
   inbox status change. If anything fails, nothing is written and the row goes
   back to pending (or to failed after MAX_ATTEMPTS).
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from . import documents, interview, limits, reminders, research, transcribe
from .config import Settings
from .embeddings import Embedder, EmbeddingError
from .llm import LLM
from .schema import Schema, load_schema
from .store import Store, Tx

log = logging.getLogger("processor")

SEARCH_LIMIT = 30
THIN_RESULTS = 5   # fewer keyword hits than this triggers one broader retry (text-only mode)
RECENT_PER_TYPE = 15
CONTEXT_PROJECTS = 80
CONTEXT_PEOPLE = 200
CONTEXT_OPEN_TASKS = 100
CONTEXT_DECISIONS = 30
BODY_CHARS_FOR_ANSWER = 1500


# --- helpers -------------------------------------------------------------------

def record_ref(schema: Schema, rec: dict) -> str:
    tdef = schema.types.get(rec["type"])
    prefix = tdef.prefix if tdef else rec["type"].upper()
    return f"{prefix}-{rec['seq']}"


def _tz(tenant: dict) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def local_label(moment: dt.datetime, tz: ZoneInfo) -> str:
    """'Tuesday 2026-11-03 00:12 (Asia/Kolkata)': weekday included so the model
    never has to work out which day of the week a date falls on."""
    m = moment.astimezone(tz)
    return f"{m:%A %Y-%m-%d %H:%M} ({tz.key})"


def with_weekday(value: Any) -> Any:
    """'2026-11-11' -> '2026-11-11 (Wednesday)'. Models get weekdays wrong when
    left to work them out, so every date they see carries its weekday."""
    if isinstance(value, str) and len(value) >= 10:
        try:
            d = dt.date.fromisoformat(value[:10])
        except ValueError:
            return value
        return f"{value} ({d:%A})"
    return value


def dated_fields(schema: Schema, type_key: str, fields: dict) -> dict:
    tdef = schema.types.get(type_key)
    if tdef is None:
        return fields
    return {k: (with_weekday(v) if k in tdef.fields and tdef.fields[k].type == "date" else v)
            for k, v in fields.items()}


def _is_uuid(s: Any) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except ValueError:
        return False


def _reply_target(source_message_id: str | None) -> int | None:
    # "telegram:<chat_id>:<message_id>"
    if source_message_id and source_message_id.startswith("telegram:"):
        tail = source_message_id.rsplit(":", 1)[-1]
        return int(tail) if tail.lstrip("-").isdigit() else None
    return None


# --- fan-out (log path) --------------------------------------------------------

def fanout_context(tx: Tx, tenant_id: uuid.UUID) -> dict[str, list[dict]]:
    projects = tx._all(
        "SELECT id, title, fields->>'Status' AS note FROM records WHERE tenant_id = %s AND type = 'project' "
        "AND coalesce(fields->>'Status', '') <> 'archived' ORDER BY updated_at DESC LIMIT %s",
        (tenant_id, CONTEXT_PROJECTS))
    people = tx._all(
        "SELECT id, title, concat_ws(', ', fields->>'Relationship', fields->>'Company') AS note "
        "FROM records WHERE tenant_id = %s AND type = 'person' ORDER BY updated_at DESC LIMIT %s",
        (tenant_id, CONTEXT_PEOPLE))
    tasks = tx._all(
        "SELECT id, seq, title, fields FROM records WHERE tenant_id = %s AND type = 'task' "
        "AND coalesce(fields->>'Status', '') NOT IN ('done', 'dropped') "
        "ORDER BY updated_at DESC LIMIT %s", (tenant_id, CONTEXT_OPEN_TASKS))
    open_tasks = [{"id": t["id"], "title": t["title"], "note": "; ".join(filter(None, [
        f"TSK-{t['seq']}", t["fields"].get("Status"),
        f"due {with_weekday(t['fields']['Due Date'])}" if t["fields"].get("Due Date") else None]))}
        for t in tasks]
    decisions = tx._all(
        "SELECT id, title, concat('DEC-', seq) AS note FROM records WHERE tenant_id = %s "
        "AND type = 'decision' ORDER BY updated_at DESC LIMIT %s", (tenant_id, CONTEXT_DECISIONS))
    return {"Projects": projects, "People": people, "Open tasks": open_tasks,
            "Recent decisions": decisions}


class FanoutExecutor:
    """Turns an LLM plan into validated records inside one transaction."""

    def __init__(self, tx: Tx, schema: Schema, tenant: dict, inbox: dict) -> None:
        self.tx, self.schema, self.tenant, self.inbox = tx, schema, tenant, inbox
        self.tid = tenant["id"]
        self.by_title: dict[tuple[str, str], uuid.UUID] = {}
        self.notes: list[str] = []
        self.written: list[str] = []

    def run(self, plan: dict) -> str:
        for u in plan.get("updates", []) or []:
            if isinstance(u, dict):
                try:
                    self._update(u)
                except (KeyError, TypeError, ValueError) as e:
                    self.notes.append(f"skipped an update: {e}")
        writes = [w for w in plan.get("writes", []) if isinstance(w, dict)]
        # People and projects first, so other writes can reference them by title.
        order = {"person": 0, "project": 1}
        writes.sort(key=lambda w: order.get(self._type_key(w) or "", 2))
        for w in writes:
            try:
                self._one(w)
            except Exception as e:  # a single bad write is reported, not fatal
                if isinstance(e, (KeyError, TypeError, ValueError)):
                    self.notes.append(f"skipped a {w.get('type')} write: {e}")
                else:
                    raise
        summary = plan.get("processor_notes") or ""
        parts = [summary.strip()] if summary else []
        if self.written:
            parts.append("Wrote: " + ", ".join(self.written))
        if self.notes:
            parts.append("Notes: " + "; ".join(self.notes))
        return "\n".join(parts) or "no actionable content"

    def _update(self, u: dict) -> None:
        """Change an existing record (e.g. mark a task done). User-edited fields stay as they are."""
        rid = str(u.get("id") or "")
        if not _is_uuid(rid):
            self.notes.append(f"update target {rid!r} is not a record ID")
            return
        rec = self.tx.record(self.tid, uuid.UUID(rid))
        if rec is None:
            self.notes.append(f"update target {rid} not found")
            return
        fields = self._resolve_relations(rec["type"], dict(u.get("fields") or {}))
        tdef = self.schema.types[rec["type"]]
        if tdef.child:
            for f in tdef.child.human_only:
                if fields.pop(f, None) is not None:
                    self.notes.append(f"{tdef.label}.{f} is human-only; not set")
        if not fields:
            return
        self.notes += self.tx.update_record(self.tid, self.schema, rec["id"], fields)
        self.written.append(f"{record_ref(self.schema, rec)} (updated)")

    def _type_key(self, w: dict) -> str | None:
        t = self.schema.type(str(w.get("type") or w.get("database") or ""))
        return t.key if t else None

    def _one(self, w: dict) -> None:
        key = self._type_key(w)
        if key is None:
            self.notes.append(f"unknown type {w.get('type')!r}")
            return
        if key == "event":                            # calendar events come only from the calendar sync
            self.notes.append("skipped an event write: calendar events come from the connected calendar")
            return
        tdef = self.schema.types[key]
        fields = dict(w.get("fields") or {})
        title = str(w.get("title") or fields.pop(tdef.title_field, "") or "").strip()
        fields.pop(tdef.title_field, None)
        if not title:
            self.notes.append(f"{tdef.label} write without a title skipped")
            return
        if tdef.child:
            for f in tdef.child.human_only:
                if fields.pop(f, None) is not None:
                    self.notes.append(f"{tdef.label}.{f} is human-only; not set")
        fields = self._resolve_relations(key, fields)
        if tdef.child and "Project" not in fields:
            pid = self._find_one("project", tdef.child.parent_project)
            if pid:
                fields["Project"] = [str(pid)]
            else:
                self.notes.append(f"parent project {tdef.child.parent_project!r} not found for {tdef.label}")

        existing = self._existing_for(key, title, fields)
        if existing is not None:
            dropped = self.tx.update_record(self.tid, self.schema, existing["id"], fields)
            self._remember(key, title, existing["id"])
            self.written.append(f"{record_ref(self.schema, existing)} (updated)")
            self.notes += dropped
            return

        rec, dropped = self.tx.create_record(self.tid, self.schema, key, title=title, fields=fields,
                                             source_inbox_id=self.inbox["id"])
        self._remember(key, title, rec["id"])
        self.written.append(record_ref(self.schema, rec))
        self.notes += dropped

    def _existing_for(self, key: str, title: str, fields: dict) -> dict | None:
        """Records that should be updated rather than duplicated."""
        if key == "person":
            matches = self.tx.find_by_title(self.tid, "person", title)
            if len(matches) == 1:
                p = matches[0]
                local_day = self.inbox["captured_at"].astimezone(_tz(self.tenant)).date()
                fields.setdefault("Last Contact", local_day.isoformat())
                new_ctx = fields.get("Context")
                if new_ctx and p["fields"].get("Context") and new_ctx not in p["fields"]["Context"]:
                    fields["Context"] = f"{p['fields']['Context']}\n{new_ctx}"
                return p
            if len(matches) > 1:
                self.notes.append(f"several people named {title!r}; created a new one")
        if key == "project":
            matches = self.tx.find_by_title(self.tid, "project", title)
            if len(matches) == 1:
                return matches[0]
        if key == "library" and fields.get("Status") == "done":
            matches = [m for m in self.tx.find_by_title(self.tid, "library", title)
                       if m["fields"].get("Status") != "done"]
            if len(matches) == 1:
                return matches[0]
        return None

    def _resolve_relations(self, key: str, fields: dict) -> dict:
        tdef = self.schema.types[key]
        out = dict(fields)
        for fname, fdef in tdef.relation_fields.items():
            if fname not in out:
                continue
            refs = out[fname] if isinstance(out[fname], list) else [out[fname]]
            ids: list[str] = []
            for ref in refs:
                if _is_uuid(ref):
                    ids.append(str(ref))
                    continue
                rid = self.by_title.get((fdef.to or "", str(ref).strip().lower())) \
                    or self._find_one(fdef.to or "", str(ref))
                if rid:
                    ids.append(str(rid))
                else:
                    self.notes.append(f"{tdef.label}.{fname}: no {fdef.to} named {ref!r}")
            if ids:
                out[fname] = ids
            else:
                del out[fname]
        return out

    def _find_one(self, type_key: str, title: str) -> uuid.UUID | None:
        hit = self.by_title.get((type_key, title.strip().lower()))
        if hit:
            return hit
        matches = self.tx.find_by_title(self.tid, type_key, title)
        return matches[0]["id"] if len(matches) == 1 else None

    def _remember(self, key: str, title: str, rid: uuid.UUID) -> None:
        self.by_title[(key, title.strip().lower())] = rid


# --- query path ----------------------------------------------------------------

RRF_K = 60


def fuse(*ranked_lists: list[dict], limit: int = SEARCH_LIMIT) -> list[dict]:
    """Reciprocal rank fusion: merge result lists by rank, not by raw score,
    so full-text and vector scores never need to be on the same scale."""
    score: dict[uuid.UUID, float] = {}
    rows: dict[uuid.UUID, dict] = {}
    for lst in ranked_lists:
        for rank, r in enumerate(lst):
            score[r["id"]] = score.get(r["id"], 0.0) + 1.0 / (RRF_K + rank + 1)
            rows.setdefault(r["id"], r)
    ordered = sorted(score, key=lambda i: score[i], reverse=True)[:limit]
    return [rows[i] for i in ordered]


def gather_records(tx: Tx, schema: Schema, tenant_id: uuid.UUID, plan: dict,
                   query_vector: list[float] | None = None) -> list[dict]:
    """Retrieve records for an answer.

    plan["broader"] (set by broaden_if_thin) is a second keyword list searched
    across all types, because a thin first result often means the planner
    picked the wrong types as well as the wrong words.
    """
    types = [t for t in plan.get("types", []) if t in schema.types] or None
    text_hits = tx.search_text(tenant_id, plan["search"], types=types, limit=SEARCH_LIMIT) \
        if plan.get("search") else []
    broader_hits = tx.search_text(tenant_id, plan["broader"], limit=SEARCH_LIMIT) \
        if plan.get("broader") else []
    vector_hits = tx.search_vector(tenant_id, query_vector, types=types, limit=SEARCH_LIMIT) \
        if query_vector is not None else []
    found: dict[uuid.UUID, dict] = {r["id"]: r for r in fuse(text_hits, vector_hits, broader_hits)}
    if len(found) < 10:
        for t in types or []:
            for r in tx.list_records(tenant_id, t, limit=RECENT_PER_TYPE):
                found.setdefault(r["id"], r)
    titles: dict[uuid.UUID, str] = {}
    out: list[dict] = []
    for r in found.values():
        links = []
        for l in tx.links_from(r["id"]):
            if l["to_id"] not in titles:
                target = tx.record(tenant_id, l["to_id"])
                titles[l["to_id"]] = f"{record_ref(schema, target)} {target['title']}" if target else "?"
            links.append(f"{l['field']}: {titles[l['to_id']]}")
        body = r.get("body") or ""
        out.append({
            "id": record_ref(schema, r), "type": schema.types[r["type"]].label if r["type"] in schema.types else r["type"],
            "title": r["title"], "fields": dated_fields(schema, r["type"], r["fields"]),
            **({"body": body[:BODY_CHARS_FOR_ANSWER]} if body else {}),
            **({"links": links} if links else {}),
            "created": with_weekday(r["created_at"].date().isoformat()),
            "updated": with_weekday(r["updated_at"].date().isoformat()),
        })
    return out


def broaden_if_thin(store: Store, llm: LLM, schema: Schema, tenant_id: uuid.UUID,
                    question: str, plan: dict) -> dict:
    """If the planned keywords find too little, ask once for different words.

    Only used without embeddings (vector search always returns a full list).
    Skipped for recency/status questions, which have no keywords. A failed
    retry is not fatal: the answer goes ahead with what was found.
    """
    if not plan.get("search"):
        return plan
    types = [t for t in plan.get("types", []) if t in schema.types] or None
    with store.tx(tenant_id) as tx:
        hits = tx.search_text(tenant_id, plan["search"], types=types, limit=THIN_RESULTS)
    if len(hits) >= THIN_RESULTS:
        return plan
    try:
        broader = llm.broaden_search(question=question, tried=plan["search"],
                                     found=[h["title"] for h in hits])
    except Exception as e:
        log.warning("broader search failed, answering with what was found: %s", e)
        return plan
    return {**plan, "broader": broader} if broader else plan


# --- reminders -------------------------------------------------------------------

def confirm_reminders(tx: Tx, schema: Schema, tenant: dict, row: dict) -> None:
    """If this message set a reminder, say exactly when it will fire, so a
    misread time ("2" meaning 2 am?) can be corrected right away."""
    tz = reminders.tzinfo(tenant["timezone"])
    now = dt.datetime.now(dt.timezone.utc).astimezone(tz)
    lines = []
    prefix = schema.types["task"].prefix if "task" in schema.types else "TSK"
    for r in tx.reminders_set_now(tenant["id"], row["id"]):
        at = reminders.parse_at(r["remind_at"], tz)
        if at is None:
            continue
        when = reminders.human(at, now)
        if at < now:
            when += " (that's already past, so it will go out right away)"
        lines.append(f"I'll remind you at {when}: {r['title']} ({prefix}-{r['seq']})")
    if lines:
        tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body="Got it. " + "\n".join(lines),
                      inbox_id=row["id"], reply_to_message_id=_reply_target(row["source_message_id"]))


# --- files (PDFs) ----------------------------------------------------------------

MAX_EXCERPTS = 6


def document_excerpts(tx: Tx, schema: Schema, tenant_id: uuid.UUID, plan: dict, question: str,
                      exclude_inbox_id: uuid.UUID | None = None) -> list[dict]:
    """Passages from the user's PDFs and voice notes that match the question's words
    (never the question's own voice note)."""
    words = " ".join(w for w in (plan.get("search"), plan.get("broader")) if w) or question
    out = []
    for c in tx.search_chunks(tenant_id, words, limit=MAX_EXCERPTS, exclude_inbox_id=exclude_inbox_id):
        tdef = schema.types.get(c["type"] or "")
        ref = f"{tdef.prefix}-{c['seq']}" if tdef and c["seq"] else None
        if c["extraction"] == "transcript":
            label = f"Voice note, {with_weekday(c['created_at'].date().isoformat())}"
        else:
            label = f"[{ref}] {c['title']}" if ref else c["filename"]
        out.append({"document_excerpt": True, "document": label,
                    "file": c["filename"], "page": c["page"], "text": c["content"]})
    return out


def _digest_text(reading: dict, filename: str, pages: int) -> str:
    lines = [f"[Attached file: {filename}, {pages} page{'s' if pages != 1 else ''}]"]
    for label, key in (("Title", "title"), ("Kind", "kind"), ("Date", "date"), ("From", "from"),
                       ("Summary", "summary")):
        if reading.get(key):
            lines.append(f"{label}: {reading[key]}")
    if reading.get("key_points"):
        lines.append("Key points:\n" + "\n".join(f"- {k}" for k in reading["key_points"]))
    if reading.get("actions"):
        lines.append("Asks of the owner:\n" + "\n".join(f"- {a}" for a in reading["actions"]))
    return "\n".join(lines)


def read_file(store: Store, llm: LLM, row: dict, att: dict) -> tuple[dict | None, int, str]:
    """Extract and read one PDF. Returns (reading or None, pages, problem message)."""
    tid = row["tenant_id"]
    with store.tx(tid) as tx:
        data = tx.attachment(tid, att["id"], with_data=True)["data"]
    data = bytes(data)
    caption = "" if row["raw_content"].startswith("[PDF] ") else row["raw_content"]
    try:
        pages = documents.extract(data)
    except documents.DocumentError as e:
        with store.tx(tid) as tx:
            tx.save_extraction(tid, att["id"], pages=0, extraction="none", text="", chunks=[])
        return None, 0, str(e)
    if not documents.looks_scanned(pages):
        text = documents.joined(pages)
        reading = llm.read_document(filename=att["filename"], caption=caption, text=text)
        with store.tx(tid) as tx:
            tx.save_extraction(tid, att["id"], pages=len(pages), extraction="text", text=text,
                               chunks=documents.chunk(pages))
        return reading, len(pages), ""
    if len(pages) > documents.MAX_SCAN_PAGES:
        with store.tx(tid) as tx:
            tx.save_extraction(tid, att["id"], pages=len(pages), extraction="none", text="", chunks=[])
        return None, len(pages), (f"It's a scan of {len(pages)} pages; I read scans up to "
                                  f"{documents.MAX_SCAN_PAGES} pages, so I kept the file but didn't read it.")
    reading = llm.read_document(filename=att["filename"], caption=caption, pdf=data)
    text = reading.get("text") or "\n".join([reading.get("summary", ""), *reading.get("key_points", [])])
    with store.tx(tid) as tx:
        tx.save_extraction(tid, att["id"], pages=len(pages), extraction="vision", text=text[:documents.MAX_TEXT_CHARS],
                           chunks=documents.text_chunks(text))
    return reading, len(pages), ""


def process_files(store: Store, llm: LLM, row: dict, tenant: dict, schema: Schema, tz, files: list[dict]) -> str:
    """A message with PDFs: read each, file one document record per file (plus any
    tasks it calls for), link the file to its record, and reply with a summary."""
    tid = tenant["id"]
    att = files[0]                                     # one file per message (Telegram sends one)
    reading, pages, problem = read_file(store, llm, row, att)
    replies: list[str] = []
    with store.tx(tid) as tx:
        context = fanout_context(tx, tid)
    if reading is not None and "document" in schema.types:
        planner_text = f"{row['raw_content']}\n\n{_digest_text(reading, att['filename'], pages)}"
        plan = llm.plan_fanout(
            schema=schema, raw_content=planner_text, captured_local=local_label(row["captured_at"], tz),
            context=context,
            hint="This message carries a PDF (summarised above). File exactly one document record for the "
                 "file, plus tasks only for actions the message or document clearly asks of me.")
    else:
        plan = {"writes": [], "updates": [], "processor_notes": problem or "file kept, not read"}
    with store.tx(tid) as tx:
        notes = FanoutExecutor(tx, schema, tenant, row).run(plan)
        made = tx.records_from_inbox(tid, row["id"])
        docs = [r for r in made if r["type"] == "document"]
        if not docs and "document" in schema.types:    # the planner skipped it: file it anyway
            r = reading or {}
            fields = {k: v for k, v in (("Kind", r.get("kind")), ("Date", r.get("date")),
                                        ("From", r.get("from")), ("Summary", r.get("summary"))) if v}
            doc, _ = tx.create_record(tid, schema, "document", title=r.get("title") or att["filename"],
                                      fields=fields, body="\n".join(f"- {k}" for k in r.get("key_points", [])) or None,
                                      source_inbox_id=row["id"])
            docs = [doc]
            made.append(doc)
        if docs:
            tx.link_attachment(att["id"], docs[0]["id"])
        prefix = schema.types["document"].prefix if "document" in schema.types else "DOC"
        if docs:
            d = docs[0]
            replies.append(f"Saved {prefix}-{d['seq']} \u201c{d['title']}\u201d"
                           + (f" ({pages} page{'s' if pages != 1 else ''})." if pages else "."))
        if problem:
            replies.append(problem)
        elif reading and reading.get("summary"):
            replies.append(reading["summary"])
        others = [r for r in made if r["type"] != "document"]
        if others:
            replies.append("Also added:\n" + "\n".join(
                f"- {schema.types[r['type']].prefix}-{r['seq']} {r['title']}" for r in others if r["type"] in schema.types))
        tx.add_outbox(tid, chat_id=tenant["telegram_chat_id"], body="\n\n".join(replies),
                      inbox_id=row["id"], reply_to_message_id=_reply_target(row["source_message_id"]))
        extra = f"read {pages} page(s) ({att['filename']})" if reading else (problem or "file not read")
        tx.finish_inbox(row["id"], status="processed", intent="log", notes=f"{notes}\n{extra}".strip())
    return "processed"


# --- per-row orchestration -----------------------------------------------------

HEARD_CHARS = 700
FAILED_REPLY = ("Sorry, something went wrong on my side and I couldn't process that. It's saved: "
                "send it again in a bit, or tap Retry on it in the web view (/web).")


def with_heard(heard: str | None, body: str) -> str:
    """Replies to a voice note start with what was heard, so a mishearing shows."""
    if heard is None:
        return body
    shown = heard if len(heard) <= HEARD_CHARS else heard[:HEARD_CHARS].rsplit(" ", 1)[0] + " …"
    return f"Heard: \u201c{shown}\u201d\n\n{body}".strip()


def voice_text(store: Store, row: dict, tenant: dict, att: dict,
               transcriber: transcribe.Transcriber | None) -> str | None:
    """The transcript of a voice note (made now, or kept from an earlier attempt).
    None when there is nothing to process; the user has been told why."""
    tid = tenant["id"]

    def finish(body: str, status: str, notes: str) -> None:
        with store.tx(tid) as tx:
            tx.add_outbox(tid, chat_id=tenant["telegram_chat_id"], body=body, inbox_id=row["id"],
                          reply_to_message_id=_reply_target(row["source_message_id"]))
            tx.finish_inbox(row["id"], status=status, notes=notes)

    if att.get("transcript") is not None:
        text = att["transcript"]
    elif transcriber is None:
        finish("I got your voice note and saved it, but voice notes aren't switched on yet, so I can't "
               "read it. Please type it for now.", "failed", "voice note kept; transcription is off")
        return None
    else:
        with store.tx(tid) as tx:
            a = tx.attachment(tid, att["id"], with_data=True)
        try:
            result = transcriber.transcribe(bytes(a["data"]), a["mime_type"] or "audio/ogg")
        except transcribe.TranscribeError as e:
            log.warning("transcription failed for %s: %s", row["id"], e)
            finish("I saved your voice note but couldn't turn it into text. Please type it, or try again.",
                   "failed", f"voice note kept; transcription failed: {e}")
            return None
        text = (result.get("text") or "").strip()
        with store.tx(tid) as tx:
            tx.save_transcript(tid, att["id"], text=text, language=result.get("language"),
                               chunks=transcribe.chunks(text), seconds=result.get("seconds"))
    if not text:
        finish("I saved your voice note but couldn't make out any words in it.", "responded",
               "voice note kept; no speech found")
        return None
    return text


def hold(store: Store, row: dict, tenant: dict, day: dt.date, reason: str, settings: Settings) -> str:
    """Over a cap: keep the message unprocessed and tell the user (once a day)."""
    tid = tenant["id"]
    everyone = reason == limits.ALL
    with store.tx(tid) as tx:
        tx.finish_inbox(row["id"], status="failed",
                        notes="not processed: " + ("daily capacity for all users reached" if everyone
                                                   else f"daily limit reached ({reason})"))
        tx.add_usage(tid, day, held=1)
        if tx.mark_told(tid, day):
            body = ("I'm at capacity for today, so I've saved your message without processing it. "
                    if everyone else f"You've reached today's limit of {reason}. I've saved your message "
                                     "without processing it. ")
            body += "Send it again tomorrow, or tap Retry on it in the web view (/web)."
            tx.add_outbox(tid, chat_id=tenant["telegram_chat_id"], body=body, inbox_id=row["id"],
                          reply_to_message_id=_reply_target(row["source_message_id"]))
    if everyone:
        limits.tell_owner_once(store, settings, "ATYA hit today's all-users processing cap "
                               f"({settings.daily_tokens_all:,} tokens). Other users' messages are being "
                               "held until tomorrow (UTC). Raise LIMIT_DAILY_TOKENS_ALL to change it.")
    log.info("held inbox %s: %s", row["id"], reason)
    return "held"


def process_row(store: Store, llm: LLM, row: dict, settings: Settings,
                embedder: Embedder | None = None,
                transcriber: transcribe.Transcriber | None = None,
                researcher: research.Researcher | None = None) -> str:
    """Process one claimed inbox row within the spending caps. Returns the final status."""
    with store.tx(row["tenant_id"]) as tx:
        tenant = tx.tenant(row["tenant_id"])
        files = tx.attachments_for([row["id"]])
    assert tenant is not None
    day = dt.datetime.now(dt.timezone.utc).astimezone(_tz(tenant)).date()
    is_audio = lambda f: (f["mime_type"] or "").startswith("audio/")  # noqa: E731
    if not limits.is_owner(tenant, settings):
        with store.tx(tenant["id"]) as tx:
            usage = tx.usage_on(tenant["id"], day)
        reason = limits.over(usage, settings, voice=any(map(is_audio, files)),
                             pdf=any(not is_audio(f) for f in files))
        if reason is None and limits.all_over(store, settings):
            reason = limits.ALL
        if reason:
            return hold(store, row, tenant, day, reason, settings)
    before = [limits.tokens_used(x) for x in (llm, researcher)]
    status = _process_row(store, llm, row, settings, embedder, transcriber, researcher)
    after = [limits.tokens_used(x) for x in (llm, researcher)]
    spent = (sum(a[0] - b[0] for a, b in zip(after, before)), sum(a[1] - b[1] for a, b in zip(after, before)))
    counted = status != "retry"
    voice_seconds = pdf_pages = 0
    if counted and files:
        with store.tx(tenant["id"]) as tx:
            files = tx.attachments_for([row["id"]])
        voice_seconds = sum(f["seconds"] or 0 for f in files if is_audio(f) and f["transcript"] is not None)
        pdf_pages = sum(f["pages"] or 0 for f in files if not is_audio(f))
    limits.record(store, tenant["id"], day, messages=1 if counted else 0, voice_seconds=voice_seconds,
                  pdf_pages=pdf_pages, tokens=spent)
    return status


def _process_row(store: Store, llm: LLM, row: dict, settings: Settings,
                 embedder: Embedder | None = None,
                 transcriber: transcribe.Transcriber | None = None,
                 researcher: research.Researcher | None = None) -> str:
    """Process one claimed inbox row. Returns the final status."""
    raw = row["raw_content"]
    with store.tx(row["tenant_id"]) as tx:
        tenant = tx.tenant(row["tenant_id"])
    assert tenant is not None
    schema = load_schema(tenant["schema_name"])
    tz = _tz(tenant)
    now = local_label(dt.datetime.now(dt.timezone.utc), tz)

    try:
        if not raw.strip():
            with store.tx(row["tenant_id"]) as tx:
                tx.finish_inbox(row["id"], status="failed", notes="empty content")
            return "failed"

        with store.tx(row["tenant_id"]) as tx:
            files = tx.attachments_for([row["id"]])
        audio = [f for f in files if (f["mime_type"] or "").startswith("audio/")]
        heard: str | None = None
        if audio:                                     # a voice note: work on what was said
            heard = voice_text(store, row, tenant, audio[0], transcriber)
            if heard is None:
                return "responded"
            raw = heard
        elif files:                                   # a PDF is always a capture
            return process_files(store, llm, row, tenant, schema, tz, files)

        with store.tx(row["tenant_id"]) as tx:
            setup = interview.current(tx, tenant["id"])
        if setup is not None and row.get("forced_intent") is None:
            return interview_turn(store, llm, row, tenant, schema, tz, raw, heard, setup)

        with store.tx(row["tenant_id"]) as tx:
            waiting = tx.get_state(research.pending_key(tenant["id"]))
            if waiting:
                tx.set_state(research.pending_key(tenant["id"]), None)
        if waiting and dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(waiting["asked"]) \
                < dt.timedelta(hours=research.PENDING_FOR_HOURS) and row.get("forced_intent") is None:
            return research_turn(store, row, tenant, schema, tz, waiting["request"], heard, researcher, settings,
                                 llm, answers="" if research.is_skip(raw) else raw, asked_already=True)
        topic = research.explicit(raw) if row.get("forced_intent") is None else None
        if topic:                                     # "research <topic>"
            return research_turn(store, row, tenant, schema, tz, topic, heard, researcher, settings, llm)

        if row.get("forced_intent") == "log":       # the user tapped "Save as task"
            cls = {"intent": "log", "reply": ""}
        else:
            cls = llm.classify(raw)
        intent = cls["intent"]
        if intent == "research":
            return research_turn(store, row, tenant, schema, tz, raw, heard, researcher, settings, llm)
        if intent == "chat":
            with store.tx(row["tenant_id"]) as tx:
                tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=with_heard(heard, cls["reply"]),
                              inbox_id=row["id"], reply_to_message_id=_reply_target(row["source_message_id"]))
                tx.finish_inbox(row["id"], status="responded", intent="chat", notes="small talk; nothing saved")
            return "responded"
        if intent == "log":
            with store.tx(row["tenant_id"]) as tx:
                context = fanout_context(tx, tenant["id"])
            hint = ("I tapped 'Save as task' on this message: file it as a task (other records "
                    "only if the message clearly calls for them).") if row.get("forced_intent") == "log" else None
            plan = llm.plan_fanout(schema=schema, raw_content=raw,
                                   captured_local=local_label(row["captured_at"], tz), context=context,
                                   hint=hint)
            with store.tx(row["tenant_id"]) as tx:
                notes = FanoutExecutor(tx, schema, tenant, row).run(plan)
                if heard is not None:                 # say what was saved, so the transcript is visible
                    made = tx.records_from_inbox(tenant["id"], row["id"])
                    saved = "\n".join(f"- {record_ref(schema, r)} {r['title']}" for r in made)
                    tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"],
                                  body=with_heard(heard, f"Saved:\n{saved}" if made else "Noted."),
                                  inbox_id=row["id"], reply_to_message_id=_reply_target(row["source_message_id"]))
                confirm_reminders(tx, schema, tenant, row)
                tx.finish_inbox(row["id"], status="processed", notes=notes, intent="log")
            return "processed"

        plan = llm.plan_query(schema=schema, question=raw, now=now)
        query_vector = None
        if embedder is not None:
            try:
                query_vector = embedder.embed_query(raw)
            except EmbeddingError as e:  # degrade to full-text only
                log.warning("query embedding failed, using full text only: %s", e)
        if query_vector is None:
            plan = broaden_if_thin(store, llm, schema, tenant["id"], raw, plan)
        with store.tx(row["tenant_id"]) as tx:
            records = gather_records(tx, schema, tenant["id"], plan, query_vector)
            records += document_excerpts(tx, schema, tenant["id"], plan, raw, exclude_inbox_id=row["id"])
        answer = llm.answer(schema=schema, question=raw, records=records, now=now)
        # Nothing relevant found: maybe it was a to-do, not a question. Offer to save it.
        buttons = None if answer["found"] else [[("Save as task", f"save:{row['id']}")]]
        with store.tx(row["tenant_id"]) as tx:
            tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=with_heard(heard, answer["text"]),
                          inbox_id=row["id"], reply_to_message_id=_reply_target(row["source_message_id"]),
                          buttons=buttons)
            tx.finish_inbox(row["id"], status="responded", intent="query",
                            notes=f"searched {', '.join(plan.get('types', []))} for {plan.get('search')!r}"
                                  + (f", then all types for {plan['broader']!r}" if plan.get("broader") else "")
                                  + "; "
                                  f"{len(records)} records"
                                  + ("" if answer["found"] else "; nothing relevant, offered Save as task"))
        return "responded"

    except Exception as e:
        log.exception("processing failed for inbox %s (attempt %s)", row["id"], row["attempts"])
        final = row["attempts"] >= settings.max_attempts
        with store.tx(row["tenant_id"]) as tx:
            if final:
                tx.finish_inbox(row["id"], status="failed", notes=f"gave up after {row['attempts']} attempts: {e}")
                tenant_row = tx.tenant(row["tenant_id"])
                if tenant_row is not None:            # never leave the user waiting in silence
                    tx.add_outbox(row["tenant_id"], chat_id=tenant_row["telegram_chat_id"], body=FAILED_REPLY,
                                  inbox_id=row["id"], reply_to_message_id=_reply_target(row["source_message_id"]))
            else:
                tx.conn.execute("UPDATE inbox SET status = 'pending', processor_notes = %s WHERE id = %s",
                                (f"attempt {row['attempts']} failed: {e}", row["id"]))
                tx.audit(row["tenant_id"], actor="processor", action="reject", target_table="inbox",
                         target_id=row["id"], summary=f"attempt {row['attempts']} failed; will retry",
                         source_data={"error": str(e)[:1000]})
        return "failed" if final else "retry"


def interview_turn(store: Store, llm: LLM, row: dict, tenant: dict, schema: Schema, tz, raw: str,
                   heard: str | None, st: dict) -> str:
    """One answer in the setup interview: file it, then ask the next question (or wrap up)."""
    tid = tenant["id"]
    kind = interview.classify_reply(raw)
    turns = st["turns"]
    saved_line = ""
    notes = "setup interview: " + kind
    if kind == "answer":
        with store.tx(tid) as tx:
            context = fanout_context(tx, tid)
        plan = llm.plan_fanout(schema=schema, raw_content=raw, captured_local=local_label(row["captured_at"], tz),
                               context=context,
                               hint=f"This answers a setup question I was asked: \u201c{turns[-1]['q']}\u201d. "
                                    "File everything it mentions (tasks, people, projects, goals, deadlines).")
        with store.tx(tid) as tx:
            notes = FanoutExecutor(tx, schema, tenant, row).run(plan)
            made = tx.records_from_inbox(tid, row["id"])
            confirm_reminders(tx, schema, tenant, row)
        turns[-1]["a"] = raw[:4000]
        counts: dict[str, int] = {}
        for r in made:
            counts[r["type"]] = counts.get(r["type"], 0) + 1
        st["saved"] = st.get("saved", 0) + len(made)
        if counts:
            saved_line = f"Saved {interview.count_phrase(counts)} ({st['saved']} so far)."
    else:
        turns[-1]["a"] = None
    if turns[-1].get("area"):                         # a fixed question, answered or skipped, covers its area
        st["covered"] = sorted(set(st.get("covered", [])) | {turns[-1]["area"]})
    st["inbox"] = [*st.get("inbox", []), str(row["id"])]
    answered = sum(1 for t in turns if t.get("a"))
    nxt = {"done": True, "question": "", "suggest": "", "area": None}
    if kind != "stop" and len(turns) < interview.MAX_TURNS:
        try:
            got = llm.interview_next(transcript=turns, areas=interview.AREAS, still_open=interview.still_open(st),
                                     saved=st.get("saved", 0), target=interview.TARGET_RECORDS)
            st["covered"] = sorted(set(st.get("covered", [])) | set(got.get("covered", [])))
            nxt = {**got, "area": None}
        except Exception as e:  # a bad model reply: fall back to a fixed question, or wrap up
            log.warning("interview question failed: %s", e)
        if not interview.may_finish(st):              # too early to stop: always ask something
            nxt["done"] = False
            if not nxt["question"]:
                area = interview.still_open(st)[0] if interview.still_open(st) else None
                nxt = {"done": False, "question": interview.fallback_question(area), "suggest": "", "area": area}
    with store.tx(tid) as tx:
        if nxt["done"]:
            ids = [uuid.UUID(i) for i in st["inbox"]]
            rows = tx._all("SELECT type, count(*) AS n FROM records WHERE tenant_id = %s "
                           "AND source_inbox_id = ANY(%s) GROUP BY type ORDER BY count(*) DESC", (tid, ids))
            counts = {r["type"]: r["n"] for r in rows}
            body = interview.summary(counts if answered else {}, nxt.get("suggest", ""))
            tx.set_state(interview.key(tid), None)
            if answered and tenant.get("brief_time") is None:     # the first habit: a brief every morning
                at = dt.time(9, 0)
                local_now = dt.datetime.now(dt.timezone.utc).astimezone(tz)
                tx.set_brief(tid, at, skip_until=local_now.date() if local_now.time() >= at else None)
        else:
            turns.append({"q": nxt["question"], **({"area": nxt["area"]} if nxt.get("area") else {})})
            st["asked"] = dt.datetime.now(dt.timezone.utc).isoformat()
            tx.set_state(interview.key(tid), st)
            body = "\n\n".join(x for x in (saved_line, nxt["question"]) if x)
        tx.add_outbox(tid, chat_id=tenant["telegram_chat_id"], body=with_heard(heard, body), inbox_id=row["id"],
                      reply_to_message_id=_reply_target(row["source_message_id"]))
        tx.finish_inbox(row["id"], status="processed", intent="log", notes=notes)
    return "processed"


RESEARCH_ACK = "Looking into it. I'll send what I find in a minute or two."


def research_turn(store: Store, row: dict, tenant: dict, schema: Schema, tz, topic: str, heard: str | None,
                  researcher: research.Researcher | None, settings: Settings, llm: LLM | None = None,
                  answers: str | None = None, asked_already: bool = False) -> str:
    """Research on the web: ask what's missing first if needed, then search from a few
    angles, write it up, reply, and keep the full report."""
    tid = tenant["id"]
    reply_to = _reply_target(row["source_message_id"])

    def done(body: str, notes: str) -> str:
        with store.tx(tid) as tx:
            tx.add_outbox(tid, chat_id=tenant["telegram_chat_id"], body=with_heard(heard, body), inbox_id=row["id"],
                          reply_to_message_id=reply_to)
            tx.finish_inbox(row["id"], status="responded", intent="research", notes=notes)
        return "responded"

    if researcher is None:
        return done("I can't search the web yet: research isn't switched on. I've kept your message.",
                    "research requested; research is off")
    local = dt.datetime.now(dt.timezone.utc).astimezone(tz)
    if not limits.is_owner(tenant, settings) and settings.daily_research:
        with store.tx(tid) as tx:
            used = tx.usage_on(tid, local.date()).get("research", 0)
        if used >= settings.daily_research:
            return done(f"You've used today's {settings.daily_research} research requests. Ask again tomorrow.",
                        "research requested; over the daily research limit")
    zone, today = tenant.get("timezone") or "UTC", f"{local:%A} {local.day} {local:%B %Y}"

    # 1. Plan: anything to ask first? Which searches?
    plan = {"ask": [], "queries": [topic], "goal": ""}
    if llm is not None and hasattr(llm, "research_plan"):
        try:
            plan = llm.research_plan(request=topic, answers=answers, allow_questions=not asked_already,
                                     timezone=zone, today=today)
        except Exception as e:  # planning is a nicety: search the request as asked
            log.warning("research plan failed, searching as asked: %s", e)
    if plan["ask"]:
        with store.tx(tid) as tx:
            tx.set_state(research.pending_key(tid), {"request": topic, "questions": plan["ask"],
                                                     "asked": dt.datetime.now(dt.timezone.utc).isoformat()})
        return done(research.ask_message(plan["ask"]), "research: asked what's missing first")

    if row["attempts"] == 1:                          # it takes a while: say so (once, not on retries)
        with store.tx(tid) as tx:
            tx.add_outbox(tid, chat_id=tenant["telegram_chat_id"], body=with_heard(heard, RESEARCH_ACK),
                          inbox_id=row["id"], reply_to_message_id=reply_to)
        heard = None                                   # already shown

    # 2. Search from each angle, at the same time.
    request = topic if answers is None else f"{topic}\n(Details they added: {answers or 'none, just search'})"
    from concurrent.futures import ThreadPoolExecutor
    def one(q: str) -> dict:
        try:
            return {"query": q, **researcher.research(q, timezone=zone, today=today)}
        except Exception as e:
            log.warning("research search %r failed: %s", q, e)
            return {"query": q, "error": str(e)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, plan["queries"]))
    findings = [f for f in results if f.get("text")]
    if not findings:
        if all("error" in f for f in results):
            raise RuntimeError("every research search failed")   # retried, then the user is told
        return done("I couldn't find anything useful on that. Try wording it differently?", "research: nothing found")

    # 3. Write it up (or, if that fails, keep the findings as they are).
    sources = research.merge_sources(findings)
    written = {"reply": findings[0]["text"], "report": "\n\n".join(f["text"] for f in findings)}
    if llm is not None and hasattr(llm, "research_write"):
        try:
            written = llm.research_write(request=request, goal=plan["goal"], findings=findings)
        except Exception as e:  # keep the raw findings rather than fail
            log.warning("research write-up failed, sending the findings: %s", e)
    full = research.with_sources(written["report"], sources)
    with store.tx(tid) as tx:
        ref = ""
        if "document" in schema.types:
            fields = {"Kind": "report", "Date": local.date().isoformat(), "From": "ATYA web research",
                      "Summary": written["reply"][:600]}
            rec, _ = tx.create_record(tid, schema, "document", title=f"Research: {topic[:90]}", fields=fields,
                                      body=f"Request: {request}\n\n{full}", source_inbox_id=row["id"])
            ref = record_ref(schema, rec)
        tx.add_usage(tid, local.date(), research=1)
    shown = research.with_sources(written["reply"], sources[:4])
    tail = f"\n\nSaved as {ref}" + (" with the full write-up." if written["report"] != written["reply"] else ".") if ref else ""
    return done(shown + tail, f"research ({len(findings)} searches): {topic[:200]}" + (f"; saved {ref}" if ref else ""))


def process_once(store: Store, llm: LLM, settings: Settings,
                 embedder: Embedder | None = None,
                 transcriber: transcribe.Transcriber | None = None,
                 researcher: research.Researcher | None = None) -> int:
    with store.tx(system=True) as tx:
        rows = tx.claim_inbox(limit=10, reclaim_after_minutes=settings.reclaim_minutes)
    for row in rows:
        process_row(store, llm, row, settings, embedder, transcriber, researcher)
    return len(rows)


def run(store: Store, llm: LLM, settings: Settings, embedder: Embedder | None = None,
        transcriber: transcribe.Transcriber | None = None,
        researcher: research.Researcher | None = None) -> None:
    log.info("processor started (search: %s)", "full text + vectors" if embedder else "full text only")
    while True:
        try:
            if process_once(store, llm, settings, embedder, transcriber, researcher) == 0:
                time.sleep(settings.poll_interval)
        except Exception:
            log.exception("processor loop error")
            time.sleep(settings.poll_interval)
