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
from .llm import LLM
from .schema import Schema, load_schema
from .store import Store, Tx

log = logging.getLogger("processor")

SEARCH_LIMIT = 30
RECENT_PER_TYPE = 15
CONTEXT_PROJECTS = 80
CONTEXT_PEOPLE = 200
BODY_CHARS_FOR_ANSWER = 1500


# --- helpers -------------------------------------------------------------------

def record_ref(schema: Schema, rec: dict) -> str:
    tdef = schema.types.get(rec["type"])
    prefix = tdef.prefix if tdef else rec["type"].upper()
    return f"{prefix}-{rec['seq']}"


def _today(tenant: dict) -> dt.date:
    try:
        tz = ZoneInfo(tenant.get("timezone") or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    return dt.datetime.now(tz).date()


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
    return {"Projects": projects, "People": people}


class FanoutExecutor:
    """Turns an LLM plan into validated records inside one transaction."""

    def __init__(self, tx: Tx, schema: Schema, tenant: dict, inbox: dict) -> None:
        self.tx, self.schema, self.tenant, self.inbox = tx, schema, tenant, inbox
        self.tid = tenant["id"]
        self.by_title: dict[tuple[str, str], uuid.UUID] = {}
        self.notes: list[str] = []
        self.written: list[str] = []

    def run(self, plan: dict) -> str:
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
                fields.setdefault("Last Contact", self.inbox["captured_at"].date().isoformat())
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

def gather_records(tx: Tx, schema: Schema, tenant_id: uuid.UUID, plan: dict) -> list[dict]:
    types = [t for t in plan.get("types", []) if t in schema.types] or None
    found: dict[uuid.UUID, dict] = {}
    if plan.get("search"):
        for r in tx.search_text(tenant_id, plan["search"], types=types, limit=SEARCH_LIMIT):
            found[r["id"]] = r
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
            "title": r["title"], "fields": r["fields"],
            **({"body": body[:BODY_CHARS_FOR_ANSWER]} if body else {}),
            **({"links": links} if links else {}),
            "created": r["created_at"].date().isoformat(), "updated": r["updated_at"].date().isoformat(),
        })
    return out


# --- per-row orchestration -----------------------------------------------------

def process_row(store: Store, llm: LLM, row: dict, settings: Settings) -> str:
    """Process one claimed inbox row. Returns the final status."""
    raw = row["raw_content"]
    with store.tx() as tx:
        tenant = tx.tenant(row["tenant_id"])
    assert tenant is not None
    schema = load_schema(tenant["schema_name"])
    today = _today(tenant).isoformat()

    try:
        if not raw.strip():
            with store.tx() as tx:
                tx.finish_inbox(row["id"], status="failed", notes="empty content")
            return "failed"

        intent = llm.classify(raw)
        if intent == "log":
            with store.tx() as tx:
                context = fanout_context(tx, tenant["id"])
            plan = llm.plan_fanout(schema=schema, raw_content=raw,
                                   captured_at=row["captured_at"].isoformat(), context=context)
            with store.tx() as tx:
                notes = FanoutExecutor(tx, schema, tenant, row).run(plan)
                tx.finish_inbox(row["id"], status="processed", notes=notes, intent="log")
            return "processed"

        plan = llm.plan_query(schema=schema, question=raw, today=today)
        with store.tx() as tx:
            records = gather_records(tx, schema, tenant["id"], plan)
        answer = llm.answer(schema=schema, question=raw, records=records, today=today)
        with store.tx() as tx:
            tx.add_outbox(tenant["id"], chat_id=tenant["telegram_chat_id"], body=answer,
                          inbox_id=row["id"], reply_to_message_id=_reply_target(row["source_message_id"]))
            tx.finish_inbox(row["id"], status="responded", intent="query",
                            notes=f"searched {', '.join(plan.get('types', []))} for {plan.get('search')!r}; "
                                  f"{len(records)} records")
        return "responded"

    except Exception as e:
        log.exception("processing failed for inbox %s (attempt %s)", row["id"], row["attempts"])
        final = row["attempts"] >= settings.max_attempts
        with store.tx() as tx:
            if final:
                tx.finish_inbox(row["id"], status="failed", notes=f"gave up after {row['attempts']} attempts: {e}")
            else:
                tx.conn.execute("UPDATE inbox SET status = 'pending', processor_notes = %s WHERE id = %s",
                                (f"attempt {row['attempts']} failed: {e}", row["id"]))
                tx.audit(row["tenant_id"], actor="processor", action="reject", target_table="inbox",
                         target_id=row["id"], summary=f"attempt {row['attempts']} failed; will retry",
                         source_data={"error": str(e)[:1000]})
        return "failed" if final else "retry"


def process_once(store: Store, llm: LLM, settings: Settings) -> int:
    with store.tx() as tx:
        rows = tx.claim_inbox(limit=10, reclaim_after_minutes=settings.reclaim_minutes)
    for row in rows:
        process_row(store, llm, row, settings)
    return len(rows)


def run(store: Store, llm: LLM, settings: Settings) -> None:
    log.info("processor started")
    while True:
        try:
            if process_once(store, llm, settings) == 0:
                time.sleep(settings.poll_interval)
        except Exception:
            log.exception("processor loop error")
            time.sleep(settings.poll_interval)
