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


# --- per-row orchestration -----------------------------------------------------

def process_row(store: Store, llm: LLM, row: dict, settings: Settings,
                embedder: Embedder | None = None) -> str:
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

        if row.get("forced_intent") == "log":       # the user tapped "Save as task"
            cls = {"intent": "log", "reply": ""}
        else:
            cls = llm.classify(raw)
        intent = cls["intent"]
        if intent == "chat":
            with store.tx(row["tenant_id"]) as tx:
                tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=cls["reply"],
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
        answer = llm.answer(schema=schema, question=raw, records=records, now=now)
        # Nothing relevant found: maybe it was a to-do, not a question. Offer to save it.
        buttons = None if answer["found"] else [[("Save as task", f"save:{row['id']}")]]
        with store.tx(row["tenant_id"]) as tx:
            tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=answer["text"],
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
            else:
                tx.conn.execute("UPDATE inbox SET status = 'pending', processor_notes = %s WHERE id = %s",
                                (f"attempt {row['attempts']} failed: {e}", row["id"]))
                tx.audit(row["tenant_id"], actor="processor", action="reject", target_table="inbox",
                         target_id=row["id"], summary=f"attempt {row['attempts']} failed; will retry",
                         source_data={"error": str(e)[:1000]})
        return "failed" if final else "retry"


def process_once(store: Store, llm: LLM, settings: Settings,
                 embedder: Embedder | None = None) -> int:
    with store.tx(system=True) as tx:
        rows = tx.claim_inbox(limit=10, reclaim_after_minutes=settings.reclaim_minutes)
    for row in rows:
        process_row(store, llm, row, settings, embedder)
    return len(rows)


def run(store: Store, llm: LLM, settings: Settings, embedder: Embedder | None = None) -> None:
    log.info("processor started (search: %s)", "full text + vectors" if embedder else "full text only")
    while True:
        try:
            if process_once(store, llm, settings, embedder) == 0:
                time.sleep(settings.poll_interval)
        except Exception:
            log.exception("processor loop error")
            time.sleep(settings.poll_interval)
