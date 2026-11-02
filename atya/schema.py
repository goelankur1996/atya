"""Load a tenant schema (schema/<name>.yaml) and validate record writes against it.

Replaces v1's parsing of the Notion "System Schema" page. The YAML is checked
for internal consistency at load time, so a broken schema fails at startup
rather than mid-processing.
"""

from __future__ import annotations

import datetime as dt
import functools
import math
from dataclasses import dataclass, field
from typing import Any

import yaml

from .config import SCHEMA_DIR

FIELD_TYPES = {"text", "number", "date", "url", "email", "select", "multi_select", "checkbox", "relation"}


class SchemaError(Exception):
    """The schema file itself is invalid."""


@dataclass(frozen=True)
class FieldDef:
    name: str
    type: str
    options: tuple[str, ...] = ()
    deprecated: tuple[str, ...] = ()
    to: str | None = None          # relation target type key
    many: bool = False             # relation cardinality
    default: Any = None
    description: str = ""


@dataclass(frozen=True)
class ChildSpec:
    parent_project: str
    date_field: str
    trigger: str
    agent_fillable: tuple[str, ...]
    human_only: tuple[str, ...]    # human_only + human_only_unless_explicit
    only_if_stated: tuple[str, ...]


@dataclass(frozen=True)
class TypeDef:
    key: str
    label: str
    prefix: str
    description: str
    title_field: str
    verbatim_body: bool
    fields: dict[str, FieldDef]
    child: ChildSpec | None

    @property
    def relation_fields(self) -> dict[str, FieldDef]:
        return {n: f for n, f in self.fields.items() if f.type == "relation"}


@dataclass
class ValidationResult:
    fields: dict[str, Any] = field(default_factory=dict)        # non-relation values, coerced
    links: dict[str, list[str]] = field(default_factory=dict)   # relation field -> record ids
    dropped: list[str] = field(default_factory=list)            # human-readable reasons


@dataclass(frozen=True)
class Schema:
    name: str
    version: int
    vocabularies: dict[str, dict]
    types: dict[str, TypeDef]
    rules_hard: str
    rules_routing: str
    rules_lenses: str

    # --- lookup ------------------------------------------------------------

    def type(self, key_or_label: str) -> TypeDef | None:
        """Find a type by key ('task') or label ('Tasks'), case-insensitive."""
        needle = (key_or_label or "").strip().lower()
        for t in self.types.values():
            if needle in (t.key.lower(), t.label.lower()):
                return t
        return None

    @property
    def child_types(self) -> list[TypeDef]:
        return [t for t in self.types.values() if t.child]

    # --- validation --------------------------------------------------------

    def validate(self, type_key: str, raw: dict[str, Any], *,
                 allow_deprecated: bool = False) -> ValidationResult:
        """Coerce and check field values for one write.

        Mirrors v1 behaviour: invalid or unknown values are dropped (and
        reported) rather than failing the whole write, so a best-effort
        extraction still lands. The title field is handled by the caller.
        """
        tdef = self.types[type_key]
        out = ValidationResult()
        for name, value in (raw or {}).items():
            if name == tdef.title_field:
                continue
            fdef = tdef.fields.get(name)
            if fdef is None:
                out.dropped.append(f"unknown field {name!r} for {tdef.label}")
                continue
            if value is None or value == "" or value == []:
                continue
            try:
                if fdef.type == "relation":
                    ids = value if isinstance(value, list) else [value]
                    ids = [str(i) for i in ids if i]
                    if not fdef.many and len(ids) > 1:
                        out.dropped.append(f"{name}: only one link allowed, kept the first")
                        ids = ids[:1]
                    out.links[name] = ids
                else:
                    out.fields[name] = _coerce(fdef, value, allow_deprecated)
            except ValueError as e:
                out.dropped.append(f"{name}: {e}")
        return out

    # --- prompt rendering --------------------------------------------------

    def render_types(self, *, include_children: bool = True) -> str:
        """Plain-text registry of types and fields for LLM prompts."""
        lines: list[str] = []
        for t in self.types.values():
            if t.child and not include_children:
                continue
            lines.append(f"## {t.label} (type: {t.key})")
            lines.append(t.description.strip())
            lines.append(f"- {t.title_field} (title)")
            if t.verbatim_body:
                lines.append("- body: VERBATIM inbox text (never rewritten)")
            for f in t.fields.values():
                lines.append(f"- {f.name}: {_describe(f, self)}")
            lines.append("")
        return "\n".join(lines).strip()

    def render_children(self) -> str:
        lines: list[str] = []
        for t in self.child_types:
            c = t.child
            assert c is not None
            lines.append(f"- {t.label} (type: {t.key}); parent project: {c.parent_project}; "
                         f"date field: {c.date_field}")
            lines.append(f"  trigger: {c.trigger.strip()}")
            lines.append(f"  agent-fillable: {', '.join(c.agent_fillable)}")
            if c.only_if_stated:
                lines.append(f"  only if stated: {', '.join(c.only_if_stated)}")
            if c.human_only:
                lines.append(f"  human-only (never set): {', '.join(c.human_only)}")
        return "\n".join(lines)

    def render_vocabularies(self) -> str:
        lines = []
        for name, v in self.vocabularies.items():
            line = f"- {name}: {', '.join(v['options'])}"
            if v.get("deprecated"):
                line += f" (deprecated, never assign: {', '.join(v['deprecated'])})"
            lines.append(line)
        return "\n".join(lines)


# --- coercion -----------------------------------------------------------------

def _coerce(fdef: FieldDef, value: Any, allow_deprecated: bool) -> Any:
    t = fdef.type
    if t in ("text", "url", "email"):
        if not isinstance(value, (str, int, float)):
            raise ValueError(f"expected text, got {type(value).__name__}")
        s = str(value)
        if t == "email" and "@" not in s:
            raise ValueError(f"not an email: {s!r}")
        return s
    if t == "number":
        if isinstance(value, bool):
            raise ValueError("expected a number, got a boolean")
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"not a number: {value!r}") from None
        if not math.isfinite(num):
            raise ValueError(f"not a finite number: {value!r}")
        return num
    if t == "checkbox":
        if isinstance(value, bool):
            return value
        if str(value).lower() in ("true", "yes", "1"):
            return True
        if str(value).lower() in ("false", "no", "0"):
            return False
        raise ValueError(f"expected true/false, got {value!r}")
    if t == "date":
        return _parse_date(str(value))
    if t == "select":
        return _check_option(fdef, str(value), allow_deprecated)
    if t == "multi_select":
        vals = value if isinstance(value, list) else [value]
        return [_check_option(fdef, str(v), allow_deprecated) for v in vals]
    raise ValueError(f"unsupported field type {t}")


def _check_option(fdef: FieldDef, value: str, allow_deprecated: bool) -> str:
    if value in fdef.options:
        return value
    if value in fdef.deprecated:
        if allow_deprecated:
            return value
        raise ValueError(f"{value!r} is deprecated and cannot be assigned to new records")
    raise ValueError(f"{value!r} is not an allowed option ({', '.join(fdef.options)})")


def _parse_date(s: str) -> str:
    """Accept YYYY-MM-DD or an ISO datetime; return it normalised."""
    s = s.strip()
    try:
        if len(s) == 10:
            return dt.date.fromisoformat(s).isoformat()
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        raise ValueError(f"not an ISO date: {s!r}") from None


def _describe(f: FieldDef, schema: Schema) -> str:
    if f.type == "relation":
        target = schema.types[f.to].label if f.to else "?"
        return f"relation to {target} ({'many' if f.many else 'one'}; use record IDs)"
    if f.type in ("select", "multi_select"):
        d = f"{f.type}: {', '.join(f.options)}"
        if f.deprecated:
            d += f" (deprecated: {', '.join(f.deprecated)})"
        return d
    return f.type + (f" ({f.description})" if f.description else "")


# --- loading ------------------------------------------------------------------

@functools.lru_cache(maxsize=16)
def load_schema(name: str = "default") -> Schema:
    path = SCHEMA_DIR / f"{name}.yaml"
    if not path.exists():
        raise SchemaError(f"schema file not found: {path}")
    return parse_schema(yaml.safe_load(path.read_text()))


def parse_schema(doc: dict) -> Schema:
    errors: list[str] = []
    vocabs: dict[str, dict] = {}
    for vname, v in (doc.get("vocabularies") or {}).items():
        vocabs[vname] = {"options": [str(o) for o in v.get("options", [])],
                         "deprecated": [str(o) for o in v.get("deprecated", [])]}

    raw_types = doc.get("types") or {}
    types: dict[str, TypeDef] = {}
    prefixes: set[str] = set()
    for key, t in raw_types.items():
        fields: dict[str, FieldDef] = {}
        for fname, f in (t.get("fields") or {}).items():
            ftype = f.get("type")
            if ftype not in FIELD_TYPES:
                errors.append(f"{key}.{fname}: unknown field type {ftype!r}")
                continue
            options: list[str] = []
            deprecated: list[str] = []
            opt = f.get("options")
            if isinstance(opt, dict) and "vocab" in opt:
                vocab = vocabs.get(opt["vocab"])
                if vocab is None:
                    errors.append(f"{key}.{fname}: unknown vocabulary {opt['vocab']!r}")
                else:
                    options, deprecated = vocab["options"], vocab["deprecated"]
            elif isinstance(opt, list):
                options = [str(o) for o in opt]
            if ftype in ("select", "multi_select") and not options:
                errors.append(f"{key}.{fname}: select field has no options")
            if ftype == "relation" and f.get("to") not in raw_types:
                errors.append(f"{key}.{fname}: relation to unknown type {f.get('to')!r}")
            default = f.get("default")
            if default is not None and options and str(default) not in options:
                errors.append(f"{key}.{fname}: default {default!r} not among options")
            fields[fname] = FieldDef(
                name=fname, type=ftype, options=tuple(options), deprecated=tuple(deprecated),
                to=f.get("to"), many=bool(f.get("many", False)), default=default,
                description=str(f.get("description", "")),
            )

        child = None
        if c := t.get("child"):
            human_only = tuple(c.get("human_only", []) + c.get("human_only_unless_explicit", []))
            child = ChildSpec(
                parent_project=str(c.get("parent_project", "")),
                date_field=str(c.get("date_field", "")),
                trigger=str(c.get("trigger", "")),
                agent_fillable=tuple(c.get("agent_fillable", [])),
                human_only=human_only,
                only_if_stated=tuple(c.get("only_if_stated", [])),
            )
            for ref in (child.date_field, *child.agent_fillable, *human_only, *child.only_if_stated):
                if ref not in fields:
                    errors.append(f"{key}.child: refers to unknown field {ref!r}")
            if "Project" not in fields or fields["Project"].to != "project":
                errors.append(f"{key}.child: spine needs a Project relation to project")

        prefix = str(t.get("prefix", ""))
        if not prefix or prefix in prefixes:
            errors.append(f"{key}: prefix missing or duplicated ({prefix!r})")
        prefixes.add(prefix)
        if not t.get("title_field"):
            errors.append(f"{key}: title_field missing")

        types[key] = TypeDef(
            key=key, label=str(t.get("label", key)), prefix=prefix,
            description=str(t.get("description", "")), title_field=str(t.get("title_field", "")),
            verbatim_body=bool(t.get("verbatim_body", False)), fields=fields, child=child,
        )

    if errors:
        raise SchemaError("invalid schema:\n  " + "\n  ".join(errors))

    rules = doc.get("rules") or {}
    return Schema(
        name=str(doc.get("name", "default")), version=int(doc.get("version", 1)),
        vocabularies=vocabs, types=types,
        rules_hard=str(rules.get("hard", "")), rules_routing=str(rules.get("routing", "")),
        rules_lenses=str(rules.get("lenses", "")),
    )
