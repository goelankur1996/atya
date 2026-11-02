"""Build one user's full export as a zip (in memory).

Contents:
  README.txt          what each file is
  raw_inbox.jsonl     every message, verbatim, with timestamps and status
  records/<type>.csv  one CSV per record type (fields as columns, links as refs)
  markdown/<Type>/... one Markdown file per record, links as [[REF Title]]
  audit_log.jsonl     every change the system made, oldest first
  schema.yaml         what each type and field means
Embeddings are not included: they are derived and can be recomputed.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any

from .config import SCHEMA_DIR
from .schema import Schema
from .store import Tx

README = """ATYA export for {name}, created {when} UTC.

raw_inbox.jsonl     Every message you sent, word for word, one JSON object per line.
records/*.csv       One spreadsheet per record type (tasks, people, journal, ...).
markdown/           One Markdown file per record. Opens in Obsidian or any editor;
                    links between records are written as [[REF Title]].
audit_log.jsonl     Every change the system made to your data, oldest first.
schema.yaml         What each record type and field means.

IDs like TSK-12 are the same IDs the bot uses in its answers.
"""


def _json_default(o: Any) -> str:
    return o.isoformat() if hasattr(o, "isoformat") else str(o)


def _safe(name: str, limit: int = 60) -> str:
    name = re.sub(r"[^\w\- .]+", "", name).strip() or "untitled"
    return name[:limit]


def build_export(tx: Tx, schema: Schema, tenant: dict) -> tuple[str, bytes]:
    """Return (filename, zip bytes) for the tenant this transaction is scoped to."""
    tid = tenant["id"]
    now = datetime.now(timezone.utc)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README.format(name=tenant.get("display_name") or "you",
                                               when=now.strftime("%Y-%m-%d %H:%M")))

        inbox = tx._all("SELECT seq, source, sender, raw_content, captured_at, status, intent, "
                        "processor_notes FROM inbox WHERE tenant_id = %s ORDER BY captured_at, seq", (tid,))
        z.writestr("raw_inbox.jsonl", "".join(
            json.dumps({"id": f"INB-{r.pop('seq')}", **r}, default=_json_default, ensure_ascii=False) + "\n"
            for r in inbox))

        records = tx._all("SELECT * FROM records WHERE tenant_id = %s ORDER BY type, seq", (tid,))
        by_id = {r["id"]: r for r in records}
        links: dict[Any, list[tuple[str, Any]]] = {}
        for l in tx._all("SELECT from_id, field, to_id FROM record_links WHERE tenant_id = %s", (tid,)):
            links.setdefault(l["from_id"], []).append((l["field"], l["to_id"]))
        inbox_ref = {r["id"]: f"INB-{r['seq']}" for r in
                     tx._all("SELECT id, seq FROM inbox WHERE tenant_id = %s", (tid,))}

        def ref(r: dict) -> str:
            t = schema.types.get(r["type"])
            return f"{t.prefix if t else r['type'].upper()}-{r['seq']}"

        for type_key in sorted({r["type"] for r in records}):
            rows = [r for r in records if r["type"] == type_key]
            tdef = schema.types.get(type_key)
            label = tdef.label if tdef else type_key
            field_names = list(tdef.fields) if tdef else []
            for r in rows:
                for k in r["fields"]:
                    if k not in field_names:
                        field_names.append(k)
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["id", "title", *field_names, "links", "body", "source_message", "created", "updated"])
            for r in rows:
                link_txt = "; ".join(f"{f}: {ref(by_id[t])} {by_id[t]['title']}"
                                     for f, t in links.get(r["id"], []) if t in by_id)
                w.writerow([ref(r), r["title"],
                            *[_cell(r["fields"].get(k)) for k in field_names],
                            link_txt, r["body"] or "", inbox_ref.get(r["source_inbox_id"], ""),
                            r["created_at"].isoformat(), r["updated_at"].isoformat()])
            z.writestr(f"records/{_safe(type_key)}.csv", out.getvalue())

            for r in rows:
                md = [f"# {r['title']}", "", f"- ID: {ref(r)}", f"- Type: {label}",
                      f"- Created: {r['created_at']:%Y-%m-%d}", f"- Updated: {r['updated_at']:%Y-%m-%d}"]
                if r["source_inbox_id"] in inbox_ref:
                    md.append(f"- From message: {inbox_ref[r['source_inbox_id']]}")
                for k, v in r["fields"].items():
                    md.append(f"- {k}: {_cell(v)}")
                for f, t in links.get(r["id"], []):
                    if t in by_id:
                        md.append(f"- {f}: [[{ref(by_id[t])} {by_id[t]['title']}]]")
                if r["body"]:
                    md += ["", r["body"]]
                z.writestr(f"markdown/{_safe(label)}/{ref(r)} {_safe(r['title'])}.md", "\n".join(md) + "\n")

        audit = tx._all("SELECT actor, action, target_table, target_id, summary, source_data, at "
                        "FROM audit_log WHERE tenant_id = %s ORDER BY at, id", (tid,))
        z.writestr("audit_log.jsonl", "".join(
            json.dumps(a, default=_json_default, ensure_ascii=False) + "\n" for a in audit))

        z.writestr("schema.yaml", (SCHEMA_DIR / f"{tenant['schema_name']}.yaml").read_text())

    filename = f"atya-export-{now:%Y%m%d-%H%M}.zip"
    return filename, buf.getvalue()


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(map(str, v))
    return str(v)
