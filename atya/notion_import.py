"""One-time import of the v1 Notion Personal OS into ATYA.

    docker compose run --rm -e NOTION_TOKEN=... app python -m atya.notion_import [--dry-run]

What it copies, in order:
1. Inbox   -> inbox     (verbatim text, original timestamps, status, Inbox ID kept as INB-n)
2. Every registered database -> records (fields validated against the schema,
   Notion auto-IDs kept: Task ID 12 stays TSK-12, page body kept as body)
3. Relations -> record_links (second pass, once every record exists)
4. Outbox  -> outbox    (history only; anything still pending is imported as cancelled)
5. Audit Log -> audit_log (original timestamps)

Every imported row carries external_id 'notion:<page id>', so a re-run skips
what is already in and only adds what is new. Notion itself is only read.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg
import requests
from psycopg.types.json import Jsonb

from .config import database_url
from .migrate import migrate
from .schema import Schema, load_schema
from .store import Store, Tx

log = logging.getLogger("notion_import")

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
ACTOR = "notion_import"

INBOX_DS = "53b1a819-0217-83b5-a734-07422f148473"
OUTBOX_DS = "3dd1a819-0217-82c4-905e-07dcf246f6e9"
AUDIT_DS = "9ef1a819-0217-83ca-85e8-079e00493539"

# Record type -> Notion data source (from the System Schema page registry).
TYPE_SOURCES: dict[str, str] = {
    "person": "e281a819-0217-83df-923a-079936bb0f93",
    "project": "28a1a819-0217-8210-bc19-07854e9183a6",
    "task": "83a1a819-0217-83ec-bf77-87002ad89142",
    "thought": "8871a819-0217-82e5-8823-0727dc22b61e",
    "decision": "6101a819-0217-8259-a283-87dfc7c4a5f3",
    "library": "5131a819-0217-82ff-b04c-8735ced19e86",
    "journal": "49d1a819-0217-83ef-b5d7-079659c30386",
    "fitness": "e521a819-0217-8363-bd28-873c152585a0",
    "robotics_milestone": "3b61a819-0217-828c-b18f-870e9d7d3b0d",
    "meal": "3c51a819-0217-839c-be75-87418a7758d9",
    "vla_milestone": "a82e3237-ff2c-4760-a1f2-a6cf54a6cb48",
}

# Notion audit "Target DB" -> v2 table name.
AUDIT_TARGETS = {"inbox": "inbox", "outbox": "outbox", "none": "none"}
INBOX_SOURCES = {"slack", "email", "whatsapp", "web", "manual", "voice", "screenshot",
                 "granola", "notion_doc", "telegram"}
INBOX_STATUSES = {"pending", "processing", "processed", "failed", "deferred", "responded"}
RAW_PROPERTY_LIMIT = 2000   # v1 stored only the first 2000 chars in Raw Content


# --- Notion access -------------------------------------------------------------

class NotionError(Exception):
    pass


class NotionSource(Protocol):
    def query_all(self, ds_id: str) -> list[dict]: ...
    def page_text(self, page_id: str) -> str: ...
    def relation_ids(self, page: dict, prop: str) -> list[str]: ...


class NotionClient:
    """Read-only Notion client with polite rate limiting (~3 requests/s)."""

    def __init__(self, token: str) -> None:
        self.http = requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {token}",
                                  "Notion-Version": NOTION_VERSION,
                                  "Content-Type": "application/json"})
        self._last = 0.0
        self.calls = 0

    def _req(self, method: str, path: str, *, body: dict | None = None,
             params: dict | None = None) -> dict:
        for attempt in range(6):
            wait = 0.34 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            try:
                r = self.http.request(method, f"{NOTION_API}{path}", json=body, params=params, timeout=60)
            except requests.RequestException as e:
                if attempt == 5:
                    raise NotionError(f"{method} {path}: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 502, 503, 504):
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            if not r.ok:
                raise NotionError(f"{method} {path}: {r.status_code} {r.text[:300]}")
            return r.json()
        raise NotionError(f"{method} {path}: gave up after retries")

    def query_all(self, ds_id: str) -> list[dict]:
        out: list[dict] = []
        body: dict[str, Any] = {"page_size": 100,
                                "sorts": [{"timestamp": "created_time", "direction": "ascending"}]}
        while True:
            resp = self._req("POST", f"/data_sources/{ds_id}/query", body=body)
            out += resp.get("results", [])
            if not resp.get("has_more"):
                return out
            body["start_cursor"] = resp["next_cursor"]

    def _children(self, block_id: str) -> list[dict]:
        out: list[dict] = []
        params: dict[str, Any] = {"page_size": 100}
        while True:
            resp = self._req("GET", f"/blocks/{block_id}/children", params=params)
            out += resp.get("results", [])
            if not resp.get("has_more"):
                return out
            params["start_cursor"] = resp["next_cursor"]

    def page_text(self, page_id: str) -> str:
        return blocks_to_text(self._children(page_id), self._children)

    def relation_ids(self, page: dict, prop: str) -> list[str]:
        p = page["properties"].get(prop) or {}
        ids = [r["id"] for r in p.get("relation", [])]
        if p.get("has_more"):   # the page object lists at most 25 related pages
            ids, cursor = [], None
            while True:
                params = {"start_cursor": cursor} if cursor else None
                resp = self._req("GET", f"/pages/{page['id']}/properties/{p['id']}", params=params)
                ids += [item["relation"]["id"] for item in resp.get("results", [])]
                if not resp.get("has_more"):
                    break
                cursor = resp["next_cursor"]
        return ids


def blocks_to_text(blocks: list[dict], children_of=None, depth: int = 0) -> str:
    """Notion blocks -> plain text, keeping list markers and nesting."""
    lines: list[str] = []
    num = 0
    for b in blocks:
        t = b.get("type", "")
        data = b.get(t) or {}
        text = "".join(rt.get("plain_text", "") for rt in data.get("rich_text", []))
        pad = "  " * depth
        num = num + 1 if t == "numbered_list_item" else 0
        if t == "bulleted_list_item":
            lines.append(f"{pad}- {text}")
        elif t == "numbered_list_item":
            lines.append(f"{pad}{num}. {text}")
        elif t == "to_do":
            lines.append(f"{pad}- [{'x' if data.get('checked') else ' '}] {text}")
        elif t.startswith("heading_"):
            lines.append(f"{pad}{'#' * int(t[-1])} {text}")
        elif t == "code":
            lines.append(f"{pad}```\n{text}\n```")
        elif t in ("paragraph", "quote", "callout", "toggle"):
            lines.append(f"{pad}{text}")
        elif t == "divider":
            lines.append(f"{pad}---")
        elif text:
            lines.append(f"{pad}{text}")
        if b.get("has_children") and children_of and depth < 2 and t not in ("child_page", "child_database"):
            sub = blocks_to_text(children_of(b["id"]), children_of, depth + 1)
            if sub:
                lines.append(sub)
    return "\n".join(lines).strip()


# --- property helpers ----------------------------------------------------------

def prop_value(p: dict | None) -> Any:
    """Notion property -> plain Python value (relations handled separately)."""
    if not p:
        return None
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v or []) or None
    if t in ("select", "status"):
        return v["name"] if v else None
    if t == "multi_select":
        return [x["name"] for x in v or []] or None
    if t == "date":
        return v["start"] if v else None
    if t in ("number", "url", "email", "checkbox", "created_time", "last_edited_time"):
        return v
    if t == "unique_id":
        return v.get("number") if v else None
    return None


def title_of(page: dict) -> str:
    for p in page["properties"].values():
        if p.get("type") == "title":
            return prop_value(p) or ""
    return ""


def auto_id(page: dict) -> int | None:
    for p in page["properties"].values():
        if p.get("type") == "unique_id":
            return prop_value(p)
    return None


def norm_id(s: str | None) -> str:
    return (s or "").replace("-", "").strip().lower()


def ext(page_id: str) -> str:
    return f"notion:{norm_id(page_id)}"


def ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    if len(s) == 10:
        return dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- import --------------------------------------------------------------------

@dataclass
class Report:
    found: Counter = field(default_factory=Counter)
    imported: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    links: int = 0
    dropped: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    problems: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"{'database':<22}{'in Notion':>10}{'imported':>10}{'already in':>11}"]
        for k in self.found:
            lines.append(f"{k:<22}{self.found[k]:>10}{self.imported[k]:>10}{self.skipped[k]:>11}")
        lines.append(f"\nrelations linked: {self.links}")
        for k, c in self.dropped.items():
            for reason, n in c.most_common(5):
                lines.append(f"dropped in {k} (x{n}): {reason}")
        for p in self.problems[:30]:
            lines.append(f"problem: {p}")
        if len(self.problems) > 30:
            lines.append(f"... and {len(self.problems) - 30} more problems")
        return "\n".join(lines)


class Importer:
    def __init__(self, store: Store, notion: NotionSource, schema: Schema, tenant: dict, *,
                 dry_run: bool = False, with_bodies: bool = True) -> None:
        self.store, self.notion, self.schema, self.tenant = store, notion, schema, tenant
        self.tid = tenant["id"]
        self.dry_run, self.with_bodies = dry_run, with_bodies
        self.report = Report()
        self.inbox_ids: dict[str, Any] = {}    # normalised notion page id -> inbox uuid
        self.record_ids: dict[str, Any] = {}   # normalised notion page id -> record uuid
        self.pages: dict[str, list[dict]] = {}

    def run(self) -> Report:
        self.import_inbox()
        for key in TYPE_SOURCES:
            self.import_records(key)
        self.link_relations()
        self.import_outbox()
        self.import_audit()
        return self.report

    # -- inbox --
    def import_inbox(self) -> None:
        pages = self.notion.query_all(INBOX_DS)
        self.report.found["inbox"] = len(pages)
        for page in pages:
            pid = norm_id(page["id"])
            props = page["properties"]
            with self.store.tx(self.tid) as tx:
                existing = tx._one("SELECT id FROM inbox WHERE tenant_id = %s AND external_id = %s",
                                   (self.tid, ext(page["id"])))
            if existing:
                self.inbox_ids[pid] = existing["id"]
                self.report.skipped["inbox"] += 1
                continue
            raw = prop_value(props.get("Raw Content")) or ""
            if self.with_bodies and (not raw or len(raw) >= RAW_PROPERTY_LIMIT):
                body = self.notion.page_text(page["id"])
                if len(body) > len(raw):
                    raw = body
            if not raw.strip():
                raw = title_of(page)
            if not raw.strip():
                self.report.problems.append(f"inbox page {page['id']} has no text; skipped")
                continue
            source = prop_value(props.get("Source")) or "manual"
            if source not in INBOX_SOURCES:
                source = "notion_import"
            status = prop_value(props.get("Status")) or "processed"
            if status not in INBOX_STATUSES:
                status = "processed"
            msg_id = prop_value(props.get("Source Message ID"))
            captured = ts(prop_value(props.get("Captured At"))) or ts(page["created_time"])
            if self.dry_run:
                self.report.imported["inbox"] += 1
                continue
            with self.store.tx(self.tid) as tx:
                if msg_id and tx._one("SELECT 1 FROM inbox WHERE tenant_id = %s AND source = %s "
                                      "AND source_message_id = %s", (self.tid, source, msg_id)):
                    msg_id = None   # same channel message captured twice in v1; keep both rows
                iid = tx.add_inbox(self.tid, source=source, raw_content=raw, captured_at=captured,
                                   source_message_id=msg_id or ext(page["id"]),
                                   sender=prop_value(props.get("Sender")), status=status,
                                   actor=ACTOR, seq=auto_id(page), external_id=ext(page["id"]))
                assert iid is not None
                tx.conn.execute(
                    "UPDATE inbox SET processor_notes = %s, processed_at = %s, created_at = %s WHERE id = %s",
                    (prop_value(props.get("Processor Notes")), ts(prop_value(props.get("Processed At"))),
                     ts(page["created_time"]), iid))
            self.inbox_ids[pid] = iid
            self.report.imported["inbox"] += 1

    # -- records --
    def import_records(self, key: str) -> None:
        tdef = self.schema.types[key]
        pages = self.notion.query_all(TYPE_SOURCES[key])
        self.pages[key] = pages
        self.report.found[key] = len(pages)
        for page in pages:
            pid = norm_id(page["id"])
            with self.store.tx(self.tid) as tx:
                existing = tx._one("SELECT id FROM records WHERE tenant_id = %s AND external_id = %s",
                                   (self.tid, ext(page["id"])))
            if existing:
                self.record_ids[pid] = existing["id"]
                self.report.skipped[key] += 1
                continue
            props = page["properties"]
            title = title_of(page).strip() or "(untitled)"
            fields: dict[str, Any] = {}
            for fname, fdef in tdef.fields.items():
                if fdef.type == "relation" or fname not in props:
                    continue
                v = prop_value(props[fname])
                if v is not None and v != "":
                    fields[fname] = v
            body = None
            if key == "thought":
                body = prop_value(props.get("Content"))
            if self.with_bodies and not body:
                body = self.notion.page_text(page["id"]) or None
            source = norm_id(prop_value(props.get("Source")))
            source_inbox = self.inbox_ids.get(source)
            if self.dry_run:
                self.report.dropped[key].update(self.schema.validate(key, fields, allow_deprecated=True).dropped)
                self.report.imported[key] += 1
                continue
            with self.store.tx(self.tid) as tx:
                rec, dropped = tx.create_record(
                    self.tid, self.schema, key, title=title, fields=fields, body=body,
                    source_inbox_id=source_inbox, actor=ACTOR, allow_deprecated=True,
                    seq=auto_id(page), external_id=ext(page["id"]), enforce_verbatim=False)
                tx.conn.execute("UPDATE records SET created_at = %s, updated_at = %s WHERE id = %s",
                                (ts(page["created_time"]), ts(page["last_edited_time"]), rec["id"]))
            self.report.dropped[key].update(dropped)
            self.record_ids[pid] = rec["id"]
            self.report.imported[key] += 1

    def link_relations(self) -> None:
        if self.dry_run:
            return
        for key, pages in self.pages.items():
            rels = self.schema.types[key].relation_fields
            for page in pages:
                from_id = self.record_ids.get(norm_id(page["id"]))
                if from_id is None:
                    continue
                for fname in rels:
                    if fname not in page["properties"]:
                        continue
                    targets = [self.record_ids.get(norm_id(i)) for i in self.notion.relation_ids(page, fname)]
                    targets = [str(t) for t in targets if t]
                    if not targets:
                        continue
                    with self.store.tx(self.tid) as tx:
                        before = len(tx.links_from(from_id))
                        dropped = tx._set_links(self.tid, self.schema, from_id, key, fname, targets)
                        self.report.links += len(tx.links_from(from_id)) - before
                    if dropped:
                        self.report.dropped[key].update(dropped)

    # -- outbox --
    def import_outbox(self) -> None:
        pages = self.notion.query_all(OUTBOX_DS)
        self.report.found["outbox"] = len(pages)
        for page in pages:
            props = page["properties"]
            with self.store.tx(self.tid) as tx:
                if tx._one("SELECT 1 FROM outbox WHERE tenant_id = %s AND external_id = %s",
                           (self.tid, ext(page["id"]))):
                    self.report.skipped["outbox"] += 1
                    continue
            body = prop_value(props.get("Body")) or title_of(page)
            if not body:
                continue
            status = prop_value(props.get("Status")) or "sent"
            if status in ("pending", "sending"):
                status = "cancelled"   # never re-send old v1 replies
            chat = str(prop_value(props.get("Chat ID")) or "")
            chat_id = int(chat) if chat.lstrip("-").isdigit() else 0
            if self.dry_run:
                self.report.imported["outbox"] += 1
                continue
            with self.store.tx(self.tid) as tx:
                tx.conn.execute(
                    "INSERT INTO outbox (tenant_id, inbox_id, channel, chat_id, body, priority, status, "
                    "sent_at, error, created_at, external_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (self.tid, self.inbox_ids.get(norm_id(prop_value(props.get("Source")))),
                     prop_value(props.get("Channel")) or "telegram", chat_id, body,
                     prop_value(props.get("Priority")) if prop_value(props.get("Priority")) in ("routine", "alert") else "routine",
                     status, ts(prop_value(props.get("Sent At"))), prop_value(props.get("Send Error")),
                     ts(page["created_time"]), ext(page["id"])))
            self.report.imported["outbox"] += 1

    # -- audit --
    def import_audit(self) -> None:
        pages = self.notion.query_all(AUDIT_DS)
        self.report.found["audit_log"] = len(pages)
        for page in pages:
            props = page["properties"]
            with self.store.tx(self.tid) as tx:
                if tx._one("SELECT 1 FROM audit_log WHERE tenant_id = %s AND external_id = %s",
                           (self.tid, ext(page["id"]))):
                    self.report.skipped["audit_log"] += 1
                    continue
            action = prop_value(props.get("Action"))
            if action not in ("create", "update", "delete", "send", "reject"):
                action = "update"
            target = prop_value(props.get("Target DB")) or "none"
            if self.dry_run:
                self.report.imported["audit_log"] += 1
                continue
            with self.store.tx(self.tid) as tx:
                tx.conn.execute(
                    "INSERT INTO audit_log (tenant_id, actor, action, target_table, target_id, summary, "
                    "source_data, at, external_id) VALUES (%s,%s,%s,%s,NULL,%s,%s,%s,%s)",
                    (self.tid, f"v1:{prop_value(props.get('Actor')) or 'unknown'}", action,
                     AUDIT_TARGETS.get(target, f"v1:{target}"),
                     prop_value(props.get("Summary")) or title_of(page) or "(no summary)",
                     Jsonb({"notion_target_id": prop_value(props.get("Target ID")),
                                               "source_data": prop_value(props.get("Source Data"))}),
                     ts(page["created_time"]), ext(page["id"])))
            self.report.imported["audit_log"] += 1


def native_rows(tx: Tx, tenant_id: Any) -> int:
    row = tx._one("SELECT (SELECT count(*) FROM inbox WHERE tenant_id = %s AND external_id IS NULL) + "
                  "(SELECT count(*) FROM records WHERE tenant_id = %s AND external_id IS NULL) AS n",
                  (tenant_id, tenant_id))
    return row["n"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="read Notion and report, write nothing")
    ap.add_argument("--skip-bodies", action="store_true", help="do not fetch page bodies (much faster)")
    ap.add_argument("--allow-existing", action="store_true",
                    help="import even if this account already has messages captured in v2")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    token = os.environ.get("NOTION_TOKEN", "").strip()
    owner = os.environ.get("OWNER_TELEGRAM_USER_ID", "").strip()
    if not token or not owner.isdigit():
        sys.exit("NOTION_TOKEN and OWNER_TELEGRAM_USER_ID must be set")

    with psycopg.connect(database_url()) as conn:
        migrate(conn)
    store = Store(database_url())
    with store.tx(system=True) as tx:
        tenant = tx.tenant_by_telegram(int(owner))
        if tenant is None:
            tenant = tx.create_tenant(telegram_user_id=int(owner), telegram_chat_id=int(owner),
                                      timezone=os.environ.get("OWNER_TIMEZONE", "UTC"), actor=ACTOR)
    with store.tx(tenant["id"]) as tx:
        n = native_rows(tx, tenant["id"])
    if n and not args.allow_existing and not args.dry_run:
        sys.exit(f"This account already has {n} messages/records captured in v2 (not from Notion).\n"
                 "Importing on top would mix histories and may clash with Notion IDs (TSK-1 ...).\n"
                 "Start from an empty database (docker compose down -v) or pass --allow-existing.")

    notion = NotionClient(token)
    started = time.monotonic()
    report = Importer(store, notion, load_schema(tenant["schema_name"]), tenant,
                      dry_run=args.dry_run, with_bodies=not args.skip_bodies).run()
    print(("\nDRY RUN - nothing written\n" if args.dry_run else "\n") + report.render())
    print(f"\n{notion.calls} Notion requests in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
