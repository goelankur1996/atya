"""Postgres storage layer. Replaces v1's direct Notion calls.

Every write that matters happens inside a `Tx`, and every Tx method that
changes data writes its audit_log row in the same transaction, so a record and
its audit entry either both exist or neither does.

    store = Store(database_url)
    with store.tx(tenant_id) as tx:
        inbox_id = tx.add_inbox(tenant_id, source="telegram", raw_content=...)
"""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .schema import Schema


class StoreError(Exception):
    pass


APP_ROLE = "atya_app"


def _as_app_role(conn: psycopg.Connection) -> None:
    # Row-level security applies to this role (not to the table owner).
    conn.execute(f"SET ROLE {APP_ROLE}")
    conn.commit()


class Store:
    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 8) -> None:
        self.pool = ConnectionPool(url, min_size=min_size, max_size=max_size,
                                   kwargs={"row_factory": dict_row}, configure=_as_app_role,
                                   open=True)

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def tx(self, tenant_id: uuid.UUID | None = None, *, system: bool = False) -> Iterator["Tx"]:
        """A transaction scoped to one user, or to the whole system.

        tx(tenant_id): Postgres only shows and accepts that user's rows.
        tx(system=True): every user's rows; for queue workers and admin steps.
        tx(): no scope, sees nothing (a forgotten scope fails closed).
        """
        if tenant_id is not None and system:
            raise ValueError("pass a tenant_id or system=True, not both")
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT set_config('atya.tenant_id', %s, true), "
                             "set_config('atya.scope', %s, true)",
                             (str(tenant_id) if tenant_id else "", "system" if system else "tenant"))
                yield Tx(conn)


class Tx:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def _one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        return self.conn.execute(sql, params).fetchall()

    # --- shared --------------------------------------------------------------

    def claim_seq(self, tenant_id: uuid.UUID, scope: str, value: int) -> int:
        """Use a specific sequence number (imports keep their old IDs) and make
        sure later numbers are allocated above it."""
        self.conn.execute(
            "INSERT INTO seq_counters (tenant_id, scope, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (tenant_id, scope) DO UPDATE SET value = GREATEST(seq_counters.value, EXCLUDED.value)",
            (tenant_id, scope, value))
        return value

    def next_seq(self, tenant_id: uuid.UUID, scope: str) -> int:
        row = self._one(
            "INSERT INTO seq_counters (tenant_id, scope, value) VALUES (%s, %s, 1) "
            "ON CONFLICT (tenant_id, scope) DO UPDATE SET value = seq_counters.value + 1 "
            "RETURNING value", (tenant_id, scope))
        assert row is not None
        return row["value"]

    def audit(self, tenant_id: uuid.UUID | None, *, actor: str, action: str,
              target_table: str, target_id: uuid.UUID | None, summary: str,
              source_data: Any = None) -> None:
        self.conn.execute(
            "INSERT INTO audit_log (tenant_id, actor, action, target_table, target_id, summary, source_data) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (tenant_id, actor, action, target_table, target_id, summary[:2000],
             Jsonb(source_data) if source_data is not None else None))

    # --- tenants -------------------------------------------------------------

    def create_tenant(self, *, telegram_user_id: int | None, telegram_chat_id: int | None,
                      display_name: str | None = None, timezone: str = "UTC",
                      schema_name: str = "default", actor: str = "system",
                      status: str = "active") -> dict:
        row = self._one(
            "INSERT INTO tenants (telegram_user_id, telegram_chat_id, display_name, timezone, schema_name, status) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
            (telegram_user_id, telegram_chat_id, display_name, timezone, schema_name, status))
        assert row is not None
        self.audit(row["id"], actor=actor, action="create", target_table="tenants",
                   target_id=row["id"], summary=f"tenant created ({display_name or telegram_user_id})")
        return row

    def tenant_by_telegram(self, telegram_user_id: int) -> dict | None:
        return self._one("SELECT * FROM tenants WHERE telegram_user_id = %s", (telegram_user_id,))

    def set_tenant_status(self, tenant_id: uuid.UUID, status: str, *, actor: str = "system") -> None:
        self.conn.execute("UPDATE tenants SET status = %s WHERE id = %s", (status, tenant_id))
        self.audit(tenant_id, actor=actor, action="update", target_table="tenants",
                   target_id=tenant_id, summary=f"status set to {status}")

    def create_invite(self, created_by: uuid.UUID, code: str) -> None:
        self.conn.execute("INSERT INTO invites (code, created_by) VALUES (%s, %s)", (code, created_by))
        self.audit(created_by, actor="commands", action="create", target_table="invites",
                   target_id=None, summary="invite code created")

    def use_invite(self, code: str, tenant_id: uuid.UUID) -> bool:
        """Mark an unused invite as used by tenant_id. Needs system scope."""
        row = self._one("UPDATE invites SET used_by = %s, used_at = now() "
                        "WHERE code = %s AND used_by IS NULL RETURNING code", (tenant_id, code))
        return row is not None

    def invite_is_open(self, code: str) -> bool:
        return self._one("SELECT 1 FROM invites WHERE code = %s AND used_by IS NULL", (code,)) is not None

    def set_timezone(self, tenant_id: uuid.UUID, timezone: str, *, actor: str = "system") -> None:
        self.conn.execute("UPDATE tenants SET timezone = %s WHERE id = %s", (timezone, tenant_id))
        self.audit(tenant_id, actor=actor, action="update", target_table="tenants",
                   target_id=tenant_id, summary=f"timezone set to {timezone}")

    def tenant(self, tenant_id: uuid.UUID) -> dict | None:
        return self._one("SELECT * FROM tenants WHERE id = %s", (tenant_id,))

    # --- inbox ---------------------------------------------------------------

    def add_inbox(self, tenant_id: uuid.UUID, *, source: str, raw_content: str,
                  captured_at: dt.datetime | None = None, source_message_id: str | None = None,
                  sender: str | None = None, status: str = "pending",
                  actor: str = "receiver", seq: int | None = None,
                  external_id: str | None = None) -> uuid.UUID | None:
        """Store a raw capture verbatim. Returns None if it is a duplicate."""
        if source_message_id is not None:
            dup = self._one("SELECT id FROM inbox WHERE tenant_id = %s AND source = %s "
                            "AND source_message_id = %s", (tenant_id, source, source_message_id))
            if dup:
                return None
        seq = self.claim_seq(tenant_id, "inbox", seq) if seq else self.next_seq(tenant_id, "inbox")
        row = self._one(
            "INSERT INTO inbox (tenant_id, seq, source, source_message_id, sender, raw_content, captured_at, "
            "status, external_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant_id, seq, source, source_message_id, sender, raw_content,
             captured_at or dt.datetime.now(dt.timezone.utc), status, external_id))
        assert row is not None
        self.audit(tenant_id, actor=actor, action="create", target_table="inbox",
                   target_id=row["id"], summary=f"captured INB-{seq} from {source}")
        return row["id"]

    def claim_inbox(self, *, limit: int = 10, reclaim_after_minutes: int = 5) -> list[dict]:
        """Claim pending rows (and rows stuck in processing) for one worker.

        SKIP LOCKED lets several workers run without taking the same row.
        """
        rows = self._all(
            "UPDATE inbox SET status = 'processing', attempts = attempts + 1, claimed_at = now() "
            "WHERE id IN ("
            "  SELECT id FROM inbox WHERE status = 'pending' "
            "     OR (status = 'processing' AND claimed_at < now() - make_interval(mins => %s)) "
            "  ORDER BY captured_at, seq LIMIT %s FOR UPDATE SKIP LOCKED) "
            "RETURNING *", (reclaim_after_minutes, limit))
        # UPDATE ... RETURNING does not keep the subquery's order: messages must
        # be handled in the order they were sent (a capture before a question about it).
        return sorted(rows, key=lambda r: (r["captured_at"], r["seq"]))

    def finish_inbox(self, inbox_id: uuid.UUID, *, status: str, notes: str | None = None,
                     intent: str | None = None, actor: str = "processor") -> None:
        row = self._one(
            "UPDATE inbox SET status = %s, processor_notes = %s, intent = coalesce(%s, intent), "
            "processed_at = now() WHERE id = %s RETURNING tenant_id, seq",
            (status, notes, intent, inbox_id))
        if row is None:
            raise StoreError(f"inbox row {inbox_id} not found")
        action = "reject" if status == "failed" else "update"
        self.audit(row["tenant_id"], actor=actor, action=action, target_table="inbox",
                   target_id=inbox_id, summary=f"INB-{row['seq']} -> {status}",
                   source_data={"notes": notes} if notes else None)

    def inbox_row(self, inbox_id: uuid.UUID) -> dict | None:
        return self._one("SELECT * FROM inbox WHERE id = %s", (inbox_id,))

    # --- records -------------------------------------------------------------

    def create_record(self, tenant_id: uuid.UUID, schema: Schema, type_key: str, *,
                      title: str, fields: dict[str, Any] | None = None, body: str | None = None,
                      source_inbox_id: uuid.UUID | None = None, actor: str = "processor",
                      allow_deprecated: bool = False, seq: int | None = None,
                      external_id: str | None = None,
                      enforce_verbatim: bool = True) -> tuple[dict, list[str]]:
        """Validate and insert one record with its links. Returns (record, dropped)."""
        tdef = schema.types.get(type_key)
        if tdef is None:
            raise StoreError(f"unknown record type {type_key!r}")
        title = (title or "").strip()
        if not title:
            raise StoreError(f"{tdef.label}: title is required")

        if tdef.verbatim_body and source_inbox_id is not None and enforce_verbatim:
            inbox = self._one("SELECT raw_content FROM inbox WHERE id = %s AND tenant_id = %s",
                              (source_inbox_id, tenant_id))
            if inbox is None:
                raise StoreError(f"source inbox row {source_inbox_id} not found")
            if body is None:
                body = inbox["raw_content"]
            elif body != inbox["raw_content"]:
                raise StoreError(f"{tdef.label}: body must be the verbatim inbox text (rule 0a)")

        result = schema.validate(type_key, fields or {}, allow_deprecated=allow_deprecated)
        values = dict(result.fields)
        if external_id is None:  # defaults are for new captures, not imported history
            for fname, fdef in tdef.fields.items():
                if fdef.default is not None and fname not in values and fdef.type != "relation":
                    values[fname] = fdef.default

        scope = f"record:{type_key}"
        seq = self.claim_seq(tenant_id, scope, seq) if seq else self.next_seq(tenant_id, scope)
        rec = self._one(
            "INSERT INTO records (tenant_id, type, seq, title, body, fields, source_inbox_id, external_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (tenant_id, type_key, seq, title[:500], body, Jsonb(values), source_inbox_id, external_id))
        assert rec is not None
        dropped = list(result.dropped)
        for fname, ids in result.links.items():
            dropped += self._set_links(tenant_id, schema, rec["id"], type_key, fname, ids)
        self.audit(tenant_id, actor=actor, action="create", target_table="records",
                   target_id=rec["id"], summary=f"{tdef.prefix}-{seq} {title[:120]}",
                   source_data={"type": type_key, "fields": values, "links": result.links,
                                "dropped": dropped, "source_inbox_id": str(source_inbox_id) if source_inbox_id else None})
        return rec, dropped

    def update_record(self, tenant_id: uuid.UUID, schema: Schema, record_id: uuid.UUID,
                      fields: dict[str, Any], *, actor: str = "processor",
                      by_user: bool = False, clear: tuple[str, ...] | list[str] = ()) -> list[str]:
        """Merge field changes into a record.

        Automation never overwrites a field the user edited by hand
        (create-or-leave); a user edit marks the field as user-owned.
        clear (user edits only): fields to empty, values or links.
        Returns the list of dropped or skipped changes.
        """
        if clear and not by_user:
            raise StoreError("only user edits can clear fields")
        rec = self._one("SELECT * FROM records WHERE id = %s AND tenant_id = %s FOR UPDATE",
                        (record_id, tenant_id))
        if rec is None:
            raise StoreError(f"record {record_id} not found")
        tdef = schema.types[rec["type"]]
        changes = dict(fields)
        new_title = changes.pop(tdef.title_field, None)
        skipped: list[str] = []
        if not by_user:
            for name in list(changes):
                if name in rec["user_edited_fields"]:
                    skipped.append(f"{name}: edited by user, left unchanged")
                    del changes[name]
            if new_title is not None and tdef.title_field in rec["user_edited_fields"]:
                skipped.append(f"{tdef.title_field}: edited by user, left unchanged")
                new_title = None

        result = schema.validate(rec["type"], changes, allow_deprecated=by_user)
        cleared = [c for c in clear if c in tdef.fields]
        merged = {k: v for k, v in {**rec["fields"], **result.fields}.items()
                  if k not in cleared or tdef.fields[k].type == "relation"}
        edited = set(rec["user_edited_fields"])
        if by_user:
            edited |= set(result.fields) | set(result.links) | set(cleared)
            if new_title is not None:
                edited.add(tdef.title_field)
        self.conn.execute(
            "UPDATE records SET fields = %s, title = coalesce(%s, title), user_edited_fields = %s "
            "WHERE id = %s", (Jsonb(merged), new_title, sorted(edited), record_id))
        dropped = skipped + list(result.dropped)
        for fname in cleared:
            if tdef.fields[fname].type == "relation":
                self.conn.execute("DELETE FROM record_links WHERE from_id = %s AND field = %s",
                                  (record_id, fname))
        for fname, ids in result.links.items():
            self.conn.execute("DELETE FROM record_links WHERE from_id = %s AND field = %s",
                              (record_id, fname))
            dropped += self._set_links(tenant_id, schema, record_id, rec["type"], fname, ids)
        self.audit(tenant_id, actor=actor, action="update", target_table="records",
                   target_id=record_id,
                   summary=f"{tdef.prefix}-{rec['seq']} updated ({', '.join(sorted(result.fields) + sorted(result.links) + sorted(cleared)) or 'no changes'})",
                   source_data={"changes": result.fields, "links": result.links, "cleared": cleared,
                                "title": new_title, "dropped": dropped, "by_user": by_user})
        return dropped

    def _set_links(self, tenant_id: uuid.UUID, schema: Schema, from_id: uuid.UUID,
                   type_key: str, fname: str, ids: list[str]) -> list[str]:
        fdef = schema.types[type_key].fields[fname]
        dropped: list[str] = []
        for raw_id in ids:
            try:
                to_id = uuid.UUID(str(raw_id))
            except ValueError:
                dropped.append(f"{fname}: {raw_id!r} is not a record ID")
                continue
            target = self._one("SELECT type FROM records WHERE id = %s AND tenant_id = %s",
                               (to_id, tenant_id))
            if target is None or target["type"] != fdef.to:
                dropped.append(f"{fname}: {raw_id} is not a {fdef.to} record")
                continue
            self.conn.execute(
                "INSERT INTO record_links (tenant_id, from_id, field, to_id) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING", (tenant_id, from_id, fname, to_id))
        return dropped

    def record(self, tenant_id: uuid.UUID, record_id: uuid.UUID) -> dict | None:
        return self._one("SELECT * FROM records WHERE id = %s AND tenant_id = %s",
                         (record_id, tenant_id))

    def links_from(self, record_id: uuid.UUID) -> list[dict]:
        return self._all("SELECT field, to_id FROM record_links WHERE from_id = %s ORDER BY field",
                         (record_id,))

    def links_to(self, record_id: uuid.UUID) -> list[dict]:
        """Records that link to this one (e.g. a project's tasks)."""
        return self._all("SELECT l.field, r.id, r.type, r.seq, r.title FROM record_links l "
                         "JOIN records r ON r.id = l.from_id WHERE l.to_id = %s "
                         "ORDER BY r.type, r.updated_at DESC", (record_id,))

    def audit_for(self, tenant_id: uuid.UUID, target_id: uuid.UUID, limit: int = 50) -> list[dict]:
        return self._all("SELECT actor, action, summary, at FROM audit_log "
                         "WHERE tenant_id = %s AND target_id = %s ORDER BY at DESC, id DESC LIMIT %s",
                         (tenant_id, target_id, limit))

    def record_by_ref(self, tenant_id: uuid.UUID, type_key: str, seq: int) -> dict | None:
        return self._one("SELECT * FROM records WHERE tenant_id = %s AND type = %s AND seq = %s",
                         (tenant_id, type_key, seq))

    def count_by_type(self, tenant_id: uuid.UUID) -> dict[str, int]:
        return {r["type"]: r["n"] for r in self._all(
            "SELECT type, count(*) AS n FROM records WHERE tenant_id = %s GROUP BY type", (tenant_id,))}

    def titles_of_type(self, tenant_id: uuid.UUID, type_key: str) -> list[dict]:
        """id + title of every record of a type, for relation pickers."""
        return self._all("SELECT id, seq, title FROM records WHERE tenant_id = %s AND type = %s "
                         "ORDER BY lower(title)", (tenant_id, type_key))

    def records_from_inbox(self, tenant_id: uuid.UUID, inbox_id: uuid.UUID) -> list[dict]:
        return self._all("SELECT * FROM records WHERE tenant_id = %s AND source_inbox_id = %s "
                         "ORDER BY created_at", (tenant_id, inbox_id))

    def list_records(self, tenant_id: uuid.UUID, type_key: str, *, limit: int = 50,
                     offset: int = 0) -> list[dict]:
        return self._all("SELECT * FROM records WHERE tenant_id = %s AND type = %s "
                         "ORDER BY updated_at DESC LIMIT %s OFFSET %s",
                         (tenant_id, type_key, limit, offset))

    def find_by_title(self, tenant_id: uuid.UUID, type_key: str, title: str) -> list[dict]:
        """Case-insensitive exact title match (used for person / project dedup)."""
        return self._all("SELECT * FROM records WHERE tenant_id = %s AND type = %s "
                         "AND lower(title) = lower(%s)", (tenant_id, type_key, title.strip()))

    def search_text(self, tenant_id: uuid.UUID, query: str, *, types: list[str] | None = None,
                    limit: int = 30) -> list[dict]:
        """Full-text search over title, body and field values, best match first.

        Terms are OR-ed (any word can match) and ranked, so a natural-language
        question still finds records that share only some of its words.
        """
        return self._all(
            "SELECT r.*, ts_rank(r.search_text, q) AS rank "
            "FROM records r, "
            "     CAST(nullif(replace(plainto_tsquery('english', %s)::text, '&', '|'), '') AS tsquery) q "
            "WHERE r.tenant_id = %s AND r.search_text @@ q "
            "AND (%s::text[] IS NULL OR r.type = ANY(%s::text[])) "
            "ORDER BY rank DESC, r.updated_at DESC LIMIT %s",
            (query, tenant_id, types, types, limit))

    def set_embedding(self, record_id: uuid.UUID, vector: list[float], *,
                      if_updated_at: dt.datetime | None = None) -> bool:
        """Store an embedding. With if_updated_at, skip it if the record changed meanwhile."""
        cur = self.conn.execute(
            "UPDATE records SET embedding = %s::vector, embedded_at = now() "
            "WHERE id = %s AND (%s::timestamptz IS NULL OR updated_at = %s::timestamptz)",
            (str(list(vector)), record_id, if_updated_at, if_updated_at))
        return cur.rowcount == 1

    def records_needing_embedding(self, *, limit: int = 64) -> list[dict]:
        return self._all(
            "SELECT id, tenant_id, type, title, body, fields, updated_at FROM records "
            "WHERE embedding IS NULL OR embedded_at < updated_at ORDER BY updated_at LIMIT %s",
            (limit,))

    def search_vector(self, tenant_id: uuid.UUID, vector: list[float], *,
                      types: list[str] | None = None, limit: int = 30) -> list[dict]:
        """Nearest records by cosine distance (smaller = closer)."""
        return self._all(
            "SELECT r.*, r.embedding <=> %s::vector AS distance FROM records r "
            "WHERE r.tenant_id = %s AND r.embedding IS NOT NULL "
            "AND (%s::text[] IS NULL OR r.type = ANY(%s::text[])) "
            "ORDER BY r.embedding <=> %s::vector LIMIT %s",
            (str(list(vector)), tenant_id, types, types, str(list(vector)), limit))

    # --- outbox --------------------------------------------------------------

    def add_outbox(self, tenant_id: uuid.UUID, *, chat_id: int, body: str,
                   inbox_id: uuid.UUID | None = None, reply_to_message_id: int | None = None,
                   priority: str = "routine", actor: str = "processor") -> uuid.UUID:
        row = self._one(
            "INSERT INTO outbox (tenant_id, inbox_id, chat_id, reply_to_message_id, body, priority) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant_id, inbox_id, chat_id, reply_to_message_id, body, priority))
        assert row is not None
        self.audit(tenant_id, actor=actor, action="create", target_table="outbox",
                   target_id=row["id"], summary="reply queued",
                   source_data={"preview": body[:300]})
        return row["id"]

    def log_sent(self, tenant_id: uuid.UUID, *, chat_id: int, body: str,
                 inbox_id: uuid.UUID | None = None, actor: str = "commands") -> uuid.UUID:
        """Record a message the bot already sent directly (command replies)."""
        row = self._one(
            "INSERT INTO outbox (tenant_id, inbox_id, chat_id, body, status, sent_at) "
            "VALUES (%s, %s, %s, %s, 'sent', now()) RETURNING id",
            (tenant_id, inbox_id, chat_id, body))
        assert row is not None
        self.audit(tenant_id, actor=actor, action="send", target_table="outbox",
                   target_id=row["id"], summary="command reply sent",
                   source_data={"preview": body[:300]})
        return row["id"]

    # --- conversation view (web) ----------------------------------------------

    def messages_page(self, tenant_id: uuid.UUID, *, kind: str = "all", query: str = "",
                      limit: int = 50, offset: int = 0) -> list[dict]:
        """Inbox rows, newest first. kind: all | log | query | chat | command | failed."""
        where = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if kind in ("log", "query", "chat", "command"):
            where.append("intent = %s")
            params.append(kind)
        elif kind == "failed":
            where.append("(status = 'failed' OR id IN (SELECT inbox_id FROM outbox "
                         "WHERE tenant_id = %s AND status = 'failed' AND inbox_id IS NOT NULL))")
            params.append(tenant_id)
        if query:
            where.append("raw_content ILIKE %s")
            params.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        return self._all(
            f"SELECT * FROM inbox WHERE {' AND '.join(where)} "  # noqa: S608 (fixed fragments)
            "ORDER BY captured_at DESC, seq DESC LIMIT %s OFFSET %s", (*params, limit, offset))

    def outbox_for(self, inbox_ids: list[uuid.UUID]) -> list[dict]:
        return self._all("SELECT * FROM outbox WHERE inbox_id = ANY(%s) ORDER BY created_at",
                         (inbox_ids,))

    def records_for(self, inbox_ids: list[uuid.UUID]) -> list[dict]:
        return self._all("SELECT id, type, seq, title, source_inbox_id FROM records "
                         "WHERE source_inbox_id = ANY(%s) ORDER BY created_at", (inbox_ids,))

    def unlinked_outbox(self, tenant_id: uuid.UUID, *, after: dt.datetime | None,
                        before: dt.datetime | None) -> list[dict]:
        """Bot messages not tied to an inbox row, between two times."""
        return self._all(
            "SELECT * FROM outbox WHERE tenant_id = %s AND inbox_id IS NULL "
            "AND (%s::timestamptz IS NULL OR created_at >= %s) "
            "AND (%s::timestamptz IS NULL OR created_at < %s) ORDER BY created_at DESC",
            (tenant_id, after, after, before, before))

    def claim_outbox(self, *, limit: int = 10, reclaim_after_minutes: int = 5) -> list[dict]:
        rows = self._all(
            "UPDATE outbox SET status = 'sending', attempts = attempts + 1 "
            "WHERE id IN ("
            "  SELECT id FROM outbox WHERE (status = 'pending' AND (scheduled_for IS NULL OR scheduled_for <= now())) "
            "  ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED) RETURNING *", (limit,))
        return sorted(rows, key=lambda r: r["created_at"])

    def finish_outbox(self, outbox_id: uuid.UUID, *, ok: bool, error: str | None = None,
                      actor: str = "sender") -> None:
        row = self._one(
            "UPDATE outbox SET status = %s, sent_at = CASE WHEN %s THEN now() END, error = %s "
            "WHERE id = %s RETURNING tenant_id",
            ("sent" if ok else "failed", ok, error, outbox_id))
        if row is None:
            raise StoreError(f"outbox row {outbox_id} not found")
        self.audit(row["tenant_id"], actor=actor, action="send" if ok else "reject",
                   target_table="outbox", target_id=outbox_id,
                   summary="delivered" if ok else f"delivery failed: {(error or '')[:200]}")

    # --- service state -------------------------------------------------------

    def get_state(self, key: str) -> Any:
        row = self._one("SELECT value FROM service_state WHERE key = %s", (key,))
        return row["value"] if row else None

    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO service_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, Jsonb(value)))

    # --- web login ------------------------------------------------------------
    # Only hashes are stored. Lookups by hash run in system scope (the tenant
    # is not known yet); everything after that runs in the tenant's scope.

    def create_login_token(self, tenant_id: uuid.UUID, token_hash: str, minutes: int) -> None:
        self.conn.execute(
            "INSERT INTO login_tokens (token_hash, tenant_id, expires_at) "
            "VALUES (%s, %s, now() + make_interval(mins => %s))", (token_hash, tenant_id, minutes))
        self.audit(tenant_id, actor="commands", action="create", target_table="login_tokens",
                   target_id=None, summary="web login link sent")

    def use_login_token(self, token_hash: str) -> uuid.UUID | None:
        """Consume a one-time token. None if unknown, used or expired."""
        row = self._one("UPDATE login_tokens SET used_at = now() WHERE token_hash = %s "
                        "AND used_at IS NULL AND expires_at > now() RETURNING tenant_id", (token_hash,))
        return row["tenant_id"] if row else None

    def create_session(self, tenant_id: uuid.UUID, session_hash: str, days: int) -> None:
        self.conn.execute(
            "INSERT INTO sessions (session_hash, tenant_id, expires_at) "
            "VALUES (%s, %s, now() + make_interval(days => %s))", (session_hash, tenant_id, days))
        self.audit(tenant_id, actor="web", action="create", target_table="sessions",
                   target_id=None, summary="signed in to the web view")

    def session_tenant(self, session_hash: str) -> uuid.UUID | None:
        row = self._one("SELECT tenant_id FROM sessions WHERE session_hash = %s AND expires_at > now()",
                        (session_hash,))
        return row["tenant_id"] if row else None

    def delete_session(self, session_hash: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE session_hash = %s", (session_hash,))

    def purge_expired_logins(self) -> None:
        self.conn.execute("DELETE FROM login_tokens WHERE expires_at < now() - interval '1 day'")
        self.conn.execute("DELETE FROM sessions WHERE expires_at < now()")

    # --- tenant deletion -----------------------------------------------------

    def delete_tenant(self, tenant_id: uuid.UUID) -> dict[str, int]:
        """Permanently remove every row for a tenant, including its audit log.

        Leaves one tenant-free operations line. S3 objects are deleted by the
        caller (export/delete flow) before this runs.
        """
        counts = {t: self._one(f"SELECT count(*) AS n FROM {t} WHERE tenant_id = %s",  # noqa: S608 (fixed table names)
                               (tenant_id,))["n"]
                  for t in ("inbox", "records", "outbox", "audit_log")}
        self.conn.execute("SELECT set_config('atya.allow_audit_purge', 'on', true)")
        self.conn.execute("DELETE FROM audit_log WHERE tenant_id = %s", (tenant_id,))
        self.conn.execute("SELECT set_config('atya.allow_audit_purge', 'off', true)")
        self.conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        self.audit(None, actor="system", action="delete", target_table="tenants", target_id=None,
                   summary="tenant deleted", source_data={"counts": counts})
        return counts
