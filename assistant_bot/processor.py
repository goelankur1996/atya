"""Worker 2 — Inbox → classify → fanout (log) | respond (query).

Also: reclaim stale `processing` rows, append-only audit log, and per-row
error isolation. Combines the old agent.py + fanout.py + responder.py + audit.py.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic

from . import claude
from .config import Config, VALID_ACTIONS, VALID_TARGET_DBS
from .notion import (
    Notion, NotionError, get_property_text, paragraph_blocks,
    rich_text_value, title_value,
)
from .schema import RulesLayer, Schema

log = logging.getLogger("processor")

BATCH_SIZE = 20
ROWS_PER_DB = 100

AUDIT_TARGET_BY_DB_NAME = {
    "Inbox": "inbox", "Outbox": "outbox", "Tasks": "tasks",
    "Projects": "projects", "Thoughts": "thoughts", "Decisions": "decisions",
    "People": "people", "Reading Queue": "reading",
}

_PROTECTED_DBS = {"inbox", "outbox", "audit log"}
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Audit log -----------------------------------------------------------

def _audit(notion: Notion, audit_ds_id: str, *, actor: str, action: str,
           target_db: str, target_id: str | None, summary: str,
           source_data: str | None = None) -> None:
    if action not in VALID_ACTIONS:
        log.warning("audit: invalid action %r, recording as 'reject'", action)
        action = "reject"
    if target_db not in VALID_TARGET_DBS:
        summary = f"[{target_db}] {summary}"
        target_db = "none"

    properties: dict = {
        "Title": {"title": title_value(summary, max_chars=200)},
        "Actor": {"rich_text": rich_text_value(actor)},
        "Action": {"select": {"name": action}},
        "Target DB": {"select": {"name": target_db}},
        "Summary": {"rich_text": rich_text_value(summary)},
    }
    if target_id:
        properties["Target ID"] = {"rich_text": rich_text_value(target_id)}
    if source_data:
        truncated = source_data if len(source_data) <= 4000 else (source_data[:4000] + " […truncated]")
        properties["Source Data"] = {"rich_text": rich_text_value(truncated)}
    try:
        notion.create_page(parent_ds_id=audit_ds_id, properties=properties)
    except NotionError as e:
        log.error("audit write failed: %s", e)


def _audit_json(obj: object) -> str:
    try:
        return json.dumps(obj, default=str, ensure_ascii=False)
    except Exception:
        return str(obj)


# --- Inbox claim / reclaim ----------------------------------------------

def _claim_filter() -> dict:
    return {"property": "Status", "select": {"equals": "pending"}}


def _stale_filter(minutes: int) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    return {"and": [
        {"property": "Status", "select": {"equals": "processing"}},
        {"timestamp": "last_edited_time", "last_edited_time": {"before": cutoff}},
    ]}


def _reclaim_stale(notion: Notion, cfg: Config, audit_ds_id: str) -> None:
    try:
        stale = notion.query(cfg.inbox_ds_id, filter=_stale_filter(cfg.reclaim_minutes),
                             page_size=20, max_pages=1)
    except NotionError as e:
        log.warning("reclaim query failed: %s", e)
        return
    for row in stale:
        try:
            notion.update_page(row["id"], {"Status": {"select": {"name": "pending"}}})
            _audit(notion, audit_ds_id,
                   actor="processor", action="update", target_db="inbox",
                   target_id=row["id"],
                   summary=f"reclaimed stale processing row after >{cfg.reclaim_minutes}m -> pending")
        except NotionError as e:
            log.warning("could not reclaim %s: %s", row["id"], e)


def _claim_row(notion: Notion, row: dict) -> bool:
    try:
        notion.update_page(row["id"], {"Status": {"select": {"name": "processing"}}})
        return True
    except NotionError as e:
        log.warning("could not claim %s: %s", row["id"], e)
        return False


def _mark_failed(notion: Notion, row_id: str) -> None:
    try:
        notion.update_page(row_id, {"Status": {"select": {"name": "failed"}}})
    except NotionError as e:
        log.error("could not mark %s failed: %s", row_id, e)


# --- Fanout (LOG path) ---------------------------------------------------

def _resolve_db(name: str, schema: Schema, db_schemas: dict[str, dict],
                child_db_schemas: dict[str, dict]) -> tuple[str | None, dict | None, str | None]:
    for n, ds_id in schema.database_registry.items():
        if n.lower() == name.lower():
            return ds_id, db_schemas.get(n), n
    for child in schema.child_registry:
        if child.name.lower() == name.lower():
            return child.data_source_id, child_db_schemas.get(child.data_source_id), child.name
    return None, None, None


def _build_property(prop_type: str, raw_val, prop_schema: dict):
    if raw_val is None or raw_val == "":
        return None
    if prop_type == "title":
        return {"title": title_value(str(raw_val))}
    if prop_type in ("rich_text", "text"):
        return {"rich_text": rich_text_value(str(raw_val))}
    if prop_type in ("select", "status"):
        opts = (prop_schema.get(prop_type) or {}).get("options") or []
        names = {o.get("name") for o in opts if o.get("name")}
        val = str(raw_val).strip()
        if names and val not in names:
            log.warning("rejecting %s value %r (not in %s)", prop_type, val, sorted(names))
            return None
        return {prop_type: {"name": val}}
    if prop_type == "multi_select":
        if not isinstance(raw_val, list):
            raw_val = [raw_val]
        opts = (prop_schema.get("multi_select") or {}).get("options") or []
        names = {o.get("name") for o in opts if o.get("name")}
        kept = [{"name": str(v).strip()} for v in raw_val
                if not names or str(v).strip() in names]
        return {"multi_select": kept} if kept else None
    if prop_type == "date":
        return {"date": {"start": str(raw_val).strip()}}
    if prop_type == "url":
        return {"url": str(raw_val).strip()}
    if prop_type == "number":
        try:
            return {"number": float(raw_val)}
        except (TypeError, ValueError):
            return None
    if prop_type == "checkbox":
        return {"checkbox": bool(raw_val)}
    if prop_type == "relation":
        if not isinstance(raw_val, list):
            raw_val = [raw_val]
        ids = [{"id": str(v).strip()} for v in raw_val if _UUID_RE.match(str(v))]
        return {"relation": ids} if ids else None
    if prop_type == "email":
        return {"email": str(raw_val).strip()}
    if prop_type == "phone_number":
        return {"phone_number": str(raw_val).strip()}
    return None


def _build_properties(raw_props: dict, ds_schema: dict) -> dict:
    schema_props = ds_schema.get("properties") or {}
    out: dict = {}
    for name, raw_val in raw_props.items():
        if name not in schema_props:
            continue
        ptype = schema_props[name]["type"]
        try:
            built = _build_property(ptype, raw_val, schema_props[name])
        except Exception as e:
            log.warning("could not build property %r (%s): %s", name, ptype, e)
            continue
        if built is not None:
            out[name] = built
    return out


def _find_existing_person(notion: Notion, people_ds_id: str, raw_props: dict) -> dict | None:
    name_val = raw_props.get("Name") or raw_props.get("Title") or raw_props.get("name")
    if not name_val:
        return None
    name = str(name_val).strip()
    if not name:
        return None
    try:
        results = notion.query(
            people_ds_id,
            filter={"or": [
                {"property": "Name", "title": {"equals": name}},
                {"property": "Name", "title": {"contains": name}},
            ]},
            page_size=5, max_pages=1,
        )
    except NotionError:
        return None
    return results[0] if results else None


def _update_existing_person(notion: Notion, page_id: str, raw_props: dict, ds_schema: dict) -> str:
    updates: dict = {}
    schema_props = ds_schema.get("properties") or {}
    if "Last Contact" in schema_props:
        updates["Last Contact"] = {"date": {"start": _now_iso()}}
    if "Context" in schema_props and raw_props.get("Context"):
        existing = notion.get_page(page_id)
        existing_ctx = get_property_text(existing, "Context")
        snippet = str(raw_props["Context"]).strip()
        if snippet and snippet not in (existing_ctx or ""):
            combined = (existing_ctx + "\n— " + snippet) if existing_ctx else snippet
            updates["Context"] = {"rich_text": rich_text_value(combined[:6000])}
    if updates:
        notion.update_page(page_id, updates)
    return page_id


def _execute_fanout_write(*, write: dict, inbox_id: str, schema: Schema,
                          db_schemas: dict[str, dict],
                          child_db_schemas: dict[str, dict],
                          notion: Notion, audit_ds_id: str) -> str | None:
    db_name = (write.get("database") or "").strip()
    if not db_name:
        return None
    if db_name.lower() in _PROTECTED_DBS:
        raise RuntimeError(f"refusing fanout write to protected DB: {db_name!r}")

    ds_id, ds_schema, resolved_name = _resolve_db(db_name, schema, db_schemas, child_db_schemas)
    if not ds_id or not ds_schema:
        raise RuntimeError(f"unknown database in plan: {db_name!r}")
    if resolved_name and resolved_name.lower() in _PROTECTED_DBS:
        raise RuntimeError(f"resolved to protected DB: {resolved_name!r}")

    raw_props = write.get("properties") or {}
    body_text = (write.get("body_text") or "").strip()

    if db_name.lower() == "people":
        existing = _find_existing_person(notion, ds_id, raw_props)
        if existing:
            page_id = _update_existing_person(notion, existing["id"], raw_props, ds_schema)
            _audit(notion, audit_ds_id,
                   actor="processor", action="update", target_db="people",
                   target_id=page_id,
                   summary="updated existing People row (matched by Name)",
                   source_data=_audit_json(raw_props))
            return page_id

    properties = _build_properties(raw_props, ds_schema)
    if "Source" in (ds_schema.get("properties") or {}):
        src_type = ds_schema["properties"]["Source"]["type"]
        if src_type in ("rich_text", "text"):
            properties["Source"] = {"rich_text": rich_text_value(inbox_id)}

    children = paragraph_blocks(body_text) if body_text else None
    resp = notion.create_page(parent_ds_id=ds_id, properties=properties, children=children)
    page_id = resp["id"]
    _audit(notion, audit_ds_id,
           actor="processor", action="create",
           target_db=AUDIT_TARGET_BY_DB_NAME.get(db_name, db_name.lower()),
           target_id=page_id,
           summary=f"created row in {db_name}",
           source_data=_audit_json(write))
    return page_id


def _run_fanout(*, inbox_row: dict, raw_content: str, schema: Schema,
                db_schemas: dict[str, dict], child_db_schemas: dict[str, dict],
                notion: Notion, anthropic_client: Anthropic, audit_ds_id: str) -> dict:
    inbox_id = inbox_row["id"]
    sender = get_property_text(inbox_row, "Sender") or "(unknown)"
    captured_at = get_property_text(inbox_row, "Captured At") or _now_iso()

    plan = claude.plan_fanout(
        raw_content=raw_content, sender=sender, captured_at=captured_at,
        inbox_id=inbox_id, schema=schema, db_schemas=db_schemas,
        child_db_schemas=child_db_schemas, client=anthropic_client,
    )

    created: list[dict] = []
    errors: list[str] = []
    for write in plan.get("writes", []):
        db_name = (write.get("database") or "").strip()
        try:
            page_id = _execute_fanout_write(
                write=write, inbox_id=inbox_id, schema=schema,
                db_schemas=db_schemas, child_db_schemas=child_db_schemas,
                notion=notion, audit_ds_id=audit_ds_id,
            )
            if page_id:
                created.append({"db": db_name, "id": page_id})
        except Exception as e:
            log.exception("fanout write to %s failed", db_name)
            errors.append(f"{db_name}: {e}")
            _audit(notion, audit_ds_id,
                   actor="processor", action="reject",
                   target_db=AUDIT_TARGET_BY_DB_NAME.get(db_name, db_name.lower()),
                   target_id=None,
                   summary=f"write rejected for {db_name}: {e}",
                   source_data=_audit_json(write))

    notion.update_page(inbox_id, {"Status": {"select": {"name": "processed"}}})
    _audit(notion, audit_ds_id,
           actor="processor", action="update", target_db="inbox",
           target_id=inbox_id,
           summary=f"processed -> created {len(created)} row(s); {len(errors)} error(s)",
           source_data=_audit_json({"created": created, "errors": errors,
                                    "plan_notes": plan.get("processor_notes", "")}))
    return {"created": created, "errors": errors}


# --- Responder (QUERY path) ----------------------------------------------

def _summarize_row(page: dict) -> dict:
    out: dict = {"id": page.get("id")}
    for name, prop in (page.get("properties") or {}).items():
        ptype = prop.get("type")
        if ptype in ("title", "rich_text", "select", "status", "date", "url",
                     "number", "auto_increment_id", "multi_select", "email",
                     "phone_number"):
            out[name] = get_property_text(page, name)
        elif ptype == "checkbox":
            out[name] = prop.get("checkbox")
        elif ptype == "relation":
            out[name] = [r.get("id") for r in (prop.get("relation") or [])]
        elif ptype in ("created_time", "last_edited_time"):
            out[name] = prop.get(ptype)
        elif ptype == "people":
            out[name] = [p.get("name") for p in (prop.get("people") or [])]
    return out


def _parse_source_msg_id(source_msg_id: str, channel: str) -> tuple[str, str]:
    if not source_msg_id:
        return "", ""
    parts = source_msg_id.split(":")
    if len(parts) >= 3 and parts[0].lower() == (channel or "").lower():
        return parts[1].strip(), parts[2].strip()
    return "", ""


def _channel_has_option(outbox_schema: dict, option_name: str) -> bool:
    props = outbox_schema.get("properties") or {}
    ch = props.get("Channel")
    if not ch or ch.get("type") != "select":
        return False
    opts = (ch.get("select") or {}).get("options") or []
    return any(o.get("name") == option_name for o in opts)


def _write_outbox(*, notion: Notion, outbox_ds_id: str, outbox_schema: dict,
                  answer_text: str, sender: str, chat_id: str, channel: str,
                  inbound_message_id: str, source_inbox_id: str) -> str:
    title = (answer_text[:60].splitlines() or [""])[0] or "answer"
    properties: dict = {
        "Title": {"title": title_value(title)},
        "Body": {"rich_text": rich_text_value(answer_text)},
        "Recipient": {"rich_text": rich_text_value(sender)},
        "Source": {"rich_text": rich_text_value(source_inbox_id)},
        "Status": {"select": {"name": "pending"}},
        "Priority": {"select": {"name": "routine"}},
    }
    if chat_id:
        properties["Chat ID"] = {"rich_text": rich_text_value(chat_id)}
    if inbound_message_id:
        properties["Source Message ID"] = {"rich_text": rich_text_value(inbound_message_id)}
    if _channel_has_option(outbox_schema, channel):
        properties["Channel"] = {"select": {"name": channel}}
    else:
        log.warning("Outbox.Channel has no %r option — writing without Channel", channel)

    schema_props = outbox_schema.get("properties") or {}
    properties = {k: v for k, v in properties.items() if k in schema_props}
    resp = notion.create_page(parent_ds_id=outbox_ds_id, properties=properties)
    return resp["id"]


def _run_responder(*, inbox_row: dict, raw_content: str, schema: Schema,
                   db_schemas: dict[str, dict], notion: Notion,
                   anthropic_client: Anthropic, audit_ds_id: str,
                   outbox_ds_id: str) -> dict:
    outbox_schema = db_schemas.get("Outbox") or {}
    inbox_id = inbox_row["id"]
    sender = get_property_text(inbox_row, "Sender") or "(unknown)"
    channel = get_property_text(inbox_row, "Source") or "telegram"
    inbox_source_msg_id = get_property_text(inbox_row, "Source Message ID")
    chat_id, inbound_message_id = _parse_source_msg_id(inbox_source_msg_id, channel)

    plan = claude.plan_query_dbs(
        question=raw_content, schema=schema, db_schemas=db_schemas,
        client=anthropic_client,
    )

    retrieved: list[dict] = []
    for db_name in plan.get("databases", []):
        ds_id = schema.data_source_id(db_name)
        if not ds_id:
            log.warning("planner picked unknown db %r — skipping", db_name)
            continue
        try:
            rows = notion.query(
                ds_id,
                sorts=[{"timestamp": "last_edited_time", "direction": "descending"}],
                page_size=ROWS_PER_DB, max_pages=1,
            )
        except NotionError as e:
            log.warning("query of %s failed: %s", db_name, e)
            rows = []
        retrieved.append({"database": db_name,
                          "rows": [_summarize_row(r) for r in rows[:ROWS_PER_DB]]})

    answer_text = claude.answer_query(
        question=raw_content, retrieved=retrieved, schema=schema,
        client=anthropic_client,
    )

    outbox_id = _write_outbox(
        notion=notion, outbox_ds_id=outbox_ds_id, outbox_schema=outbox_schema,
        answer_text=answer_text, sender=sender, chat_id=chat_id, channel=channel,
        inbound_message_id=inbound_message_id, source_inbox_id=inbox_id,
    )

    notion.update_page(inbox_id, {"Status": {"select": {"name": "responded"}}})

    _audit(notion, audit_ds_id,
           actor="processor", action="create", target_db="outbox",
           target_id=outbox_id, summary="wrote query answer to Outbox",
           source_data=_audit_json({
               "question": raw_content[:1000],
               "queried": plan.get("databases", []),
               "answer_preview": answer_text[:300],
           }))
    _audit(notion, audit_ds_id,
           actor="processor", action="update", target_db="inbox",
           target_id=inbox_id, summary="responded to query")
    return {"outbox_id": outbox_id, "queried": plan.get("databases", [])}


# --- Per-row orchestration ----------------------------------------------

def _read_full_content(notion: Notion, row: dict) -> str:
    raw = get_property_text(row, "Raw Content")
    if raw:
        return raw
    try:
        blocks = notion.get_block_children(row["id"])
        parts: list[str] = []
        for b in blocks:
            if b.get("type") == "paragraph":
                rt = b["paragraph"].get("rich_text") or []
                parts.append("".join(seg.get("plain_text", "") for seg in rt))
        return "\n".join(p for p in parts if p)
    except NotionError as e:
        log.warning("could not fetch body for %s: %s", row["id"], e)
        return ""


def _process_one(*, row: dict, notion: Notion, anthropic_client: Anthropic,
                 schema: Schema, db_schemas: dict[str, dict],
                 child_db_schemas: dict[str, dict],
                 audit_ds_id: str, outbox_ds_id: str) -> None:
    row_id = row["id"]
    raw_content = _read_full_content(notion, row)
    if not raw_content.strip():
        _mark_failed(notion, row_id)
        _audit(notion, audit_ds_id,
               actor="processor", action="reject", target_db="inbox",
               target_id=row_id, summary="empty content — marked failed")
        return

    log.info("processing %s (%d chars)", row_id[:8], len(raw_content))
    if not _claim_row(notion, row):
        return

    try:
        intent = claude.classify_intent(raw_content, anthropic_client)
    except claude.LLMError as e:
        log.warning("classifier could not parse %s: %s — leaving pending", row_id, e)
        try:
            notion.update_page(row_id, {"Status": {"select": {"name": "pending"}}})
        except NotionError:
            pass
        return
    except Exception as e:
        log.exception("classifier crashed for %s", row_id)
        _mark_failed(notion, row_id)
        _audit(notion, audit_ds_id,
               actor="processor", action="reject", target_db="inbox",
               target_id=row_id, summary=f"classifier error: {e}")
        return

    log.info("intent for %s: %s", row_id[:8], intent)

    try:
        if intent == "log":
            result = _run_fanout(
                inbox_row=row, raw_content=raw_content, schema=schema,
                db_schemas=db_schemas, child_db_schemas=child_db_schemas,
                notion=notion, anthropic_client=anthropic_client,
                audit_ds_id=audit_ds_id,
            )
            log.info("fanout %s: created=%d errors=%d", row_id[:8],
                     len(result.get("created", [])), len(result.get("errors", [])))
        else:
            result = _run_responder(
                inbox_row=row, raw_content=raw_content, schema=schema,
                db_schemas=db_schemas, notion=notion,
                anthropic_client=anthropic_client,
                audit_ds_id=audit_ds_id, outbox_ds_id=outbox_ds_id,
            )
            log.info("responded to %s via Outbox %s", row_id[:8],
                     result.get("outbox_id", "?")[:8])
    except Exception as e:
        log.exception("processing failed for %s", row_id)
        _mark_failed(notion, row_id)
        _audit(notion, audit_ds_id,
               actor="processor", action="reject", target_db="inbox",
               target_id=row_id, summary=f"row processing failed: {e}")


# --- Main loop -----------------------------------------------------------

def run(cfg: Config, notion: Notion, rules: RulesLayer) -> None:
    anthropic_client = Anthropic(api_key=cfg.anthropic_api_key)
    snapshot = rules.force_reload()
    schema = snapshot.schema
    audit_ds_id = schema.data_source_id("Audit Log")
    outbox_ds_id = schema.data_source_id("Outbox")
    if not audit_ds_id:
        log.error("no 'Audit Log' entry in schema's Database registry — abort")
        sys.exit(1)
    if not outbox_ds_id:
        log.error("no 'Outbox' entry in schema's Database registry — abort")
        sys.exit(1)

    log.info("ready. poll=%ds reclaim>%dm schema_reload>%dm",
             cfg.poll_interval, cfg.reclaim_minutes, cfg.schema_reload_minutes)

    while True:
        try:
            snapshot = rules.get()
            schema = snapshot.schema
            audit_ds_id = schema.data_source_id("Audit Log") or audit_ds_id
            outbox_ds_id = schema.data_source_id("Outbox") or outbox_ds_id

            _reclaim_stale(notion, cfg, audit_ds_id)

            pending = notion.query(
                cfg.inbox_ds_id, filter=_claim_filter(),
                sorts=[{"timestamp": "created_time", "direction": "ascending"}],
                page_size=BATCH_SIZE, max_pages=1,
            )

            for row in pending:
                _process_one(
                    row=row, notion=notion, anthropic_client=anthropic_client,
                    schema=schema, db_schemas=snapshot.db_schemas,
                    child_db_schemas=snapshot.child_db_schemas,
                    audit_ds_id=audit_ds_id, outbox_ds_id=outbox_ds_id,
                )
        except Exception:
            log.exception("top-level loop iteration error — continuing")

        time.sleep(cfg.poll_interval)
