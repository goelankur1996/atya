"""System Schema page loader + RulesLayer cache.

Merges the old schema_loader.py + rules_layer.py. Loads the Notion "System
Schema" page (database registry, child registry, vocabularies, hard rules,
routing rules, derived lenses) and keeps an in-memory snapshot that auto-
reloads on a TTL. Failure to reload keeps the previous snapshot — never blocks.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .notion import Notion, NotionError, extract_plain_text

log = logging.getLogger("schema")

# Section titles on the System Schema page
SECTION_DB_REGISTRY = "Database registry"
SECTION_CHILD_DBS = "Child databases (Project-scoped trackers)"
SUBSECTION_CHILD_REGISTRY = "Child database registry"
SECTION_VOCABS = "Shared vocabularies"
SECTION_DERIVED_LENSES = "Derived lenses (computed, not stored)"
SECTION_HARD_RULES = "Hard rules for all automation"
SECTION_ROUTING_FANOUT = "Routing rules — Inbox processor fan-out"
SECTION_CROSS_RELATIONS = "Cross-database relations"


@dataclass
class ChildDB:
    name: str
    data_source_id: str
    prefix: str
    parent_project: str
    trigger: str
    fillable_fields: str


@dataclass
class Schema:
    database_registry: dict[str, str] = field(default_factory=dict)
    child_registry: list[ChildDB] = field(default_factory=list)
    vocabularies: dict[str, str] = field(default_factory=dict)
    routing_rules_text: str = ""
    hard_rules_text: str = ""
    derived_lenses_text: str = ""
    cross_relations_text: str = ""

    def data_source_id(self, db_name: str) -> str | None:
        return self.database_registry.get(db_name)


@dataclass
class RulesSnapshot:
    schema: Schema
    db_schemas: dict[str, dict]            # registry name -> data source schema
    child_db_schemas: dict[str, dict]      # data_source_id -> data source schema
    loaded_at: float


def _block_text(block: dict) -> str:
    btype = block.get("type")
    payload = block.get(btype) or {}
    return extract_plain_text(payload.get("rich_text")) if payload else ""


def _block_to_md(block: dict) -> str:
    btype = block.get("type")
    text = _block_text(block)
    if btype == "heading_1":
        return f"# {text}"
    if btype == "heading_2":
        return f"## {text}"
    if btype == "heading_3":
        return f"### {text}"
    if btype == "bulleted_list_item":
        return f"- {text}"
    if btype == "numbered_list_item":
        return f"1. {text}"
    if btype == "to_do":
        checked = block.get("to_do", {}).get("checked", False)
        return f"- [{'x' if checked else ' '}] {text}"
    if btype == "quote":
        return f"> {text}"
    if btype == "code":
        return f"```\n{text}\n```"
    if btype == "table":
        return ""
    return text


def _parse_table(block: dict) -> list[list[str]]:
    rows: list[list[str]] = []
    for child in block.get("children", []):
        if child.get("type") != "table_row":
            continue
        cells = child.get("table_row", {}).get("cells", [])
        rows.append([extract_plain_text(c) for c in cells])
    return rows


def _parse_db_registry(rows: list[list[str]], schema: Schema) -> None:
    if not rows:
        return
    header = [c.strip().lower() for c in rows[0]]
    try:
        name_col = header.index("database")
        ds_col = header.index("data source id")
    except ValueError:
        return
    for cells in rows[1:]:
        if len(cells) <= max(name_col, ds_col):
            continue
        name, ds = cells[name_col].strip(), cells[ds_col].strip()
        if name and ds:
            schema.database_registry[name] = ds


def _parse_child_registry(rows: list[list[str]], schema: Schema) -> None:
    if not rows:
        return
    header = [c.strip().lower() for c in rows[0]]

    def col(label: str) -> int:
        try:
            return header.index(label.lower())
        except ValueError:
            return -1

    i_name = col("Child DB")
    i_ds = col("Data Source ID")
    i_prefix = col("Prefix")
    i_parent = col("Parent Project")
    i_trigger = col("Trigger condition (plain language)")
    i_fill = col("Agent-fillable fields")

    def safe(cells: list[str], idx: int) -> str:
        return cells[idx].strip() if 0 <= idx < len(cells) else ""

    for cells in rows[1:]:
        name, ds = safe(cells, i_name), safe(cells, i_ds)
        if not name or not ds:
            continue
        schema.child_registry.append(ChildDB(
            name=name, data_source_id=ds, prefix=safe(cells, i_prefix),
            parent_project=safe(cells, i_parent),
            trigger=safe(cells, i_trigger),
            fillable_fields=safe(cells, i_fill),
        ))


def _load_schema(notion: Notion, schema_page_id: str) -> Schema:
    blocks = notion.get_page_blocks_deep(schema_page_id, max_depth=3)
    schema = Schema()
    current_h2: str | None = None
    current_h3: str | None = None
    sections: dict[tuple[str | None, str | None], list[str]] = {}

    for b in blocks:
        btype = b.get("type")
        if btype == "heading_2":
            current_h2, current_h3 = _block_text(b), None
            continue
        if btype == "heading_3":
            current_h3 = _block_text(b)
            continue
        if btype == "table":
            rows = _parse_table(b)
            if current_h2 == SECTION_DB_REGISTRY:
                _parse_db_registry(rows, schema)
            elif current_h2 == SECTION_CHILD_DBS and current_h3 == SUBSECTION_CHILD_REGISTRY:
                _parse_child_registry(rows, schema)
            continue
        md = _block_to_md(b)
        if md:
            sections.setdefault((current_h2, current_h3), []).append(md)

    def collect(h2: str) -> str:
        out: list[str] = []
        for (k2, k3), lines in sections.items():
            if k2 != h2:
                continue
            if k3:
                out.append(f"### {k3}")
            out.extend(lines)
        return "\n".join(out).strip()

    schema.routing_rules_text = collect(SECTION_ROUTING_FANOUT)
    schema.hard_rules_text = collect(SECTION_HARD_RULES)
    schema.derived_lenses_text = collect(SECTION_DERIVED_LENSES)
    schema.cross_relations_text = collect(SECTION_CROSS_RELATIONS)

    for (h2, h3), lines in sections.items():
        if h2 == SECTION_VOCABS and h3:
            schema.vocabularies[h3] = "\n".join(lines).strip()

    return schema


def db_property_summary(ds_schema: dict) -> str:
    """Compact rendering of a data source's properties for LLM prompts."""
    props = ds_schema.get("properties") or {}
    lines: list[str] = []
    for name, prop in props.items():
        ptype = prop.get("type", "?")
        line = f"- {name} ({ptype})"
        if ptype in ("select", "status", "multi_select"):
            opts = (prop.get(ptype) or {}).get("options") or []
            names = [o.get("name") for o in opts if o.get("name")]
            if names:
                line += " options=" + ", ".join(f"`{o}`" for o in names)
        elif ptype == "relation":
            rel = prop.get("relation") or {}
            ds = rel.get("data_source_id") or rel.get("database_id")
            if ds:
                line += f" -> {ds}"
        lines.append(line)
    return "\n".join(lines)


class RulesLayer:
    def __init__(self, *, notion: Notion, schema_page_id: str, reload_seconds: int) -> None:
        self.notion = notion
        self.schema_page_id = schema_page_id
        self.reload_seconds = reload_seconds
        self._snapshot: RulesSnapshot | None = None

    def get(self) -> RulesSnapshot:
        if self._snapshot is None or self._is_stale():
            try:
                self._snapshot = self._load_fresh()
            except Exception as e:
                if self._snapshot is None:
                    raise
                log.warning(
                    "rules reload failed (%s) — keeping previous snapshot (%.0fs old)",
                    e, time.monotonic() - self._snapshot.loaded_at,
                )
                self._snapshot.loaded_at = time.monotonic()
        return self._snapshot

    def force_reload(self) -> RulesSnapshot:
        self._snapshot = self._load_fresh()
        return self._snapshot

    def _is_stale(self) -> bool:
        assert self._snapshot is not None
        return (time.monotonic() - self._snapshot.loaded_at) > self.reload_seconds

    def _load_fresh(self) -> RulesSnapshot:
        log.info("loading System Schema page %s", self.schema_page_id)
        schema = _load_schema(self.notion, self.schema_page_id)

        prev_dbs = self._snapshot.db_schemas if self._snapshot else {}
        prev_children = self._snapshot.child_db_schemas if self._snapshot else {}

        db_schemas: dict[str, dict] = {}
        for name, ds_id in schema.database_registry.items():
            try:
                db_schemas[name] = self.notion.get_data_source(ds_id)
            except NotionError as e:
                if name in prev_dbs:
                    db_schemas[name] = prev_dbs[name]
                    log.warning("could not refresh %s — keeping previous: %s", name, e)
                else:
                    log.error("could not load schema for %s: %s", name, e)

        child_db_schemas: dict[str, dict] = {}
        for child in schema.child_registry:
            try:
                child_db_schemas[child.data_source_id] = self.notion.get_data_source(child.data_source_id)
            except NotionError as e:
                if child.data_source_id in prev_children:
                    child_db_schemas[child.data_source_id] = prev_children[child.data_source_id]
                    log.warning("could not refresh child %s — keeping previous: %s", child.name, e)
                else:
                    log.error("could not load child schema for %s: %s", child.name, e)

        snap = RulesSnapshot(
            schema=schema,
            db_schemas=db_schemas,
            child_db_schemas=child_db_schemas,
            loaded_at=time.monotonic(),
        )
        log.info(
            "rules loaded: %d DBs (%s), %d child DBs (%s), %d vocab sections",
            len(schema.database_registry),
            ", ".join(schema.database_registry.keys()),
            len(schema.child_registry),
            ", ".join(c.name for c in schema.child_registry),
            len(schema.vocabularies),
        )
        return snap
