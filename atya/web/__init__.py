"""Worker 4: the web view. Browse, search, edit and export one user's records.

Server-rendered pages, no JavaScript. Sign-in is only through the bot: /web
sends a single-use link, and opening it (then pressing Continue) starts a
session. The Continue step exists because Telegram and other apps fetch links
to build previews; a plain GET never consumes the token.

Every request after sign-in runs in that user's database scope, so Postgres
row-level security keeps users apart even if a query here forgets to filter.
User edits are saved with by_user=True: the field is marked as user-owned and
the bot never overwrites it afterwards.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import secrets
import zoneinfo
from typing import Any

from flask import Flask, Response, abort, g, redirect, render_template, request, url_for

from ..config import Settings
from ..export import build_export
from ..schema import FieldDef, TypeDef, load_schema
from ..store import Store

log = logging.getLogger("web")

COOKIE = "atya_session"
SESSION_DAYS = 30
PAGE_SIZE = 50
MAX_PAGE = 10_000
MESSAGE_KINDS = {"all": "All", "log": "Captures", "query": "Questions", "chat": "Chat",
                 "command": "Commands", "failed": "Failed"}
# Tables a user can browse raw: (column that holds their id, sort order, what it is).
# service_state is system-wide and not shown.
RAW_TABLES = {
    "inbox": ("tenant_id", "captured_at DESC, seq DESC", "Every message you sent, verbatim, and what happened to it."),
    "outbox": ("tenant_id", "created_at DESC", "Every message the bot sent you or queued for you."),
    "records": ("tenant_id", "updated_at DESC", "Tasks, people, journal entries and the other typed records."),
    "record_links": ("tenant_id", "created_at DESC", "Links between records (task to project, thought to person)."),
    "audit_log": ("tenant_id", "at DESC, id DESC", "Every change made to your data, newest first. Append-only."),
    "tenants": ("id", "created_at DESC", "Your account."),
    "invites": ("created_by", "created_at DESC", "Invite codes you created."),
    "seq_counters": ("tenant_id", "scope", "Counters behind IDs like TSK-12."),
    "sessions": ("tenant_id", "created_at DESC", "Web sign-ins (only a hash of each is stored)."),
    "login_tokens": ("tenant_id", "expires_at DESC", "Sign-in links sent by /web (hashes only)."),
    "attachments": ("tenant_id", "created_at DESC", "Files you sent (not stored yet)."),
}
RAW_CELL_CHARS = 600
LIST_COLUMNS = ("Status", "Due Date", "Date", "Priority", "Area", "Relationship", "Type", "Mood")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_app(store: Store, settings: Settings) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    secure_cookie = bool(settings.web_base_url and settings.web_base_url.startswith("https://"))

    # --- session handling --------------------------------------------------

    @app.before_request
    def load_user() -> None:
        g.tenant = None
        g.csrf = ""
        token = request.cookies.get(COOKIE)
        if not token:
            return
        with store.tx(system=True) as tx:
            tenant_id = tx.session_tenant(_hash(token))
        if tenant_id is None:
            return
        with store.tx(tenant_id) as tx:
            tenant = tx.tenant(tenant_id)
        if tenant is None or tenant["status"] != "active":
            return
        g.tenant = tenant
        g.schema = load_schema(tenant["schema_name"])
        g.csrf = hmac.new(token.encode(), b"csrf", hashlib.sha256).hexdigest()

    @app.after_request
    def headers(resp: Response) -> Response:
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def need_login() -> None:
        if g.tenant is None:
            abort(redirect(url_for("home")))

    def check_csrf() -> None:
        sent = request.form.get("csrf", "")
        if not g.csrf or not hmac.compare_digest(sent, g.csrf):
            abort(400, "This form has expired. Go back, reload the page and try again.")

    def tx():
        return store.tx(g.tenant["id"])

    @app.context_processor
    def helpers() -> dict:
        return {"ref": ref, "fmt_time": fmt_time, "fmt_value": fmt_value, "status_label": status_label,
                "csrf": g.get("csrf", ""),
                "tenant": g.get("tenant"), "schema": g.get("schema")}

    def ref(rec: dict) -> str:
        tdef = g.schema.types.get(rec["type"])
        return f"{tdef.prefix if tdef else rec['type'].upper()}-{rec['seq']}"

    def fmt_time(ts: dt.datetime | None) -> str:
        if ts is None:
            return ""
        try:
            tz = zoneinfo.ZoneInfo(g.tenant["timezone"])
        except Exception:
            tz = dt.timezone.utc
        return ts.astimezone(tz).strftime("%a %d %b %Y, %H:%M")

    # --- sign in / out -----------------------------------------------------

    @app.get("/")
    def home():
        if g.tenant is None:
            return render_template("signed_out.html")
        with tx() as t:
            counts = t.count_by_type(g.tenant["id"])
        types = [(td, counts.get(td.key, 0)) for td in g.schema.types.values()]
        return render_template("home.html", types=types)

    @app.get("/login/<token>")
    def login_page(token: str):
        # Does not consume the token (link previews would burn it).
        return render_template("login.html", token=token)

    def same_origin_only() -> None:
        """Block cross-site form posts to /login (login CSRF): another site
        could otherwise sign a visitor into the attacker's own account."""
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(400, "Open the sign-in link directly from Telegram.")
        origin = request.headers.get("Origin")
        allowed = (settings.web_base_url or request.host_url).rstrip("/")
        if origin and origin != "null" and origin.rstrip("/") != allowed:
            abort(400, "Open the sign-in link directly from Telegram.")

    @app.post("/login")
    def login():
        same_origin_only()
        token = request.form.get("token", "")
        with store.tx(system=True) as t:
            tenant_id = t.use_login_token(_hash(token)) if token else None
        if tenant_id is None:
            return render_template("login_failed.html"), 400
        session = secrets.token_urlsafe(32)
        with store.tx(tenant_id) as t:
            t.create_session(tenant_id, _hash(session), SESSION_DAYS)
        with store.tx(system=True) as t:
            t.purge_expired_logins()
        resp = redirect(url_for("home"))
        resp.set_cookie(COOKIE, session, max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=secure_cookie, samesite="Lax")
        return resp

    @app.post("/logout")
    def logout():
        need_login()
        check_csrf()
        token = request.cookies.get(COOKIE, "")
        with tx() as t:
            t.delete_session(_hash(token))
        resp = redirect(url_for("home"))
        resp.delete_cookie(COOKIE)
        return resp

    # --- browse --------------------------------------------------------------

    @app.get("/t/<type_key>")
    def type_list(type_key: str):
        need_login()
        tdef = g.schema.types.get(type_key) or abort(404)
        page = min(MAX_PAGE, max(1, request.args.get("page", 1, type=int)))
        with tx() as t:
            rows = t.list_records(g.tenant["id"], type_key, limit=PAGE_SIZE + 1,
                                  offset=(page - 1) * PAGE_SIZE)
        cols = [c for c in LIST_COLUMNS if c in tdef.fields][:3]
        return render_template("list.html", tdef=tdef, rows=rows[:PAGE_SIZE], cols=cols, page=page,
                               more=len(rows) > PAGE_SIZE)

    @app.get("/search")
    def search():
        need_login()
        q = clean_text(request.args.get("q", ""))[:500]
        rows: list[dict] = []
        if q:
            with tx() as t:
                rows = t.search_text(g.tenant["id"], q, limit=PAGE_SIZE)
        return render_template("search.html", q=q, rows=rows)

    # --- conversation ----------------------------------------------------------

    @app.get("/messages")
    def messages():
        need_login()
        kind = request.args.get("kind", "all")
        if kind not in MESSAGE_KINDS:
            kind = "all"
        q = clean_text(request.args.get("q", ""))[:200]
        page = min(MAX_PAGE, max(1, request.args.get("page", 1, type=int)))
        tid = g.tenant["id"]
        with tx() as t:
            # One extra row on each side: the last row of the previous page and
            # the first of the next mark the time window this page covers.
            start = (page - 1) * PAGE_SIZE
            rows = t.messages_page(tid, kind=kind, query=q, limit=PAGE_SIZE + 2 if page > 1 else PAGE_SIZE + 1,
                                   offset=start - 1 if page > 1 else 0)
            prev_row = rows.pop(0) if page > 1 and rows else None
            more = len(rows) > PAGE_SIZE
            rows = rows[:PAGE_SIZE]
            ids = [r["id"] for r in rows]
            replies: dict = {}
            for o in t.outbox_for(ids):
                replies.setdefault(o["inbox_id"], []).append(o)
            filed: dict = {}
            for r in t.records_for(ids):
                filed.setdefault(r["source_inbox_id"], []).append(r)
            # Bot messages with no message of yours behind them (e.g. imported
            # from v1), placed in time order on the unfiltered view.
            extra: list[dict] = []
            if kind == "all" and not q:
                newest = prev_row["captured_at"] if prev_row else None
                oldest = rows[-1]["captured_at"] if rows and more else None
                extra = t.unlinked_outbox(tid, after=oldest, before=newest)
        items = [{"kind": "in", "at": r["captured_at"], "row": r,
                  "replies": replies.get(r["id"], []), "filed": filed.get(r["id"], [])} for r in rows]
        items += [{"kind": "out", "at": o["created_at"], "row": o} for o in extra]
        items.sort(key=lambda i: i["at"], reverse=True)
        return render_template("messages.html", items=items, kind=kind, kinds=MESSAGE_KINDS, q=q,
                               page=page, more=more)

    # --- raw database ----------------------------------------------------------

    @app.get("/db")
    def db_tables():
        need_login()
        tid = g.tenant["id"]
        with tx() as t:
            counts = {name: t.conn.execute(
                f"SELECT count(*) AS n FROM {name} WHERE {col} = %s", (tid,)).fetchone()["n"]  # noqa: S608
                for name, (col, _order, _desc) in RAW_TABLES.items()}
        return render_template("db.html", tables=RAW_TABLES, counts=counts)

    @app.get("/db/<name>")
    def db_table(name: str):
        need_login()
        if name not in RAW_TABLES:
            abort(404)
        col, order, desc = RAW_TABLES[name]
        page = min(MAX_PAGE, max(1, request.args.get("page", 1, type=int)))
        with tx() as t:
            cur = t.conn.execute(
                f"SELECT * FROM {name} WHERE {col} = %s ORDER BY {order} LIMIT %s OFFSET %s",  # noqa: S608
                (g.tenant["id"], PAGE_SIZE + 1, (page - 1) * PAGE_SIZE))
            rows = cur.fetchall()
            columns = [d.name for d in cur.description or []]
        return render_template("db_table.html", name=name, desc=desc, columns=columns,
                               rows=[[raw_cell(c, r[c]) for c in columns] for r in rows[:PAGE_SIZE]],
                               page=page, more=len(rows) > PAGE_SIZE)

    # --- one record ------------------------------------------------------------

    def load_record(ref_str: str) -> tuple[TypeDef, dict]:
        m = re.fullmatch(r"([A-Za-z_]+)-([0-9]{1,9})", ref_str)
        if not m:
            abort(404)
        prefix, seq = m.group(1).upper(), m.group(2)
        tdef = next((td for td in g.schema.types.values() if td.prefix == prefix), None)
        if tdef is None:
            abort(404)
        with tx() as t:
            rec = t.record_by_ref(g.tenant["id"], tdef.key, int(seq))
        if rec is None:
            abort(404)
        return tdef, rec

    def record_page(tdef: TypeDef, rec: dict, *, problems: list[str] | None = None, status: int = 200):
        with tx() as t:
            links: dict[str, set[str]] = {}
            for l in t.links_from(rec["id"]):
                links.setdefault(l["field"], set()).add(str(l["to_id"]))
            pickers = {name: t.titles_of_type(g.tenant["id"], f.to)
                       for name, f in tdef.relation_fields.items() if f.to}
            backlinks = t.links_to(rec["id"])
            source = t.inbox_row(rec["source_inbox_id"]) if rec["source_inbox_id"] else None
            history = t.audit_for(g.tenant["id"], rec["id"])
        fields = [(f, form_value(f, rec["fields"].get(f.name), links.get(f.name, set())))
                  for f in tdef.fields.values()]
        # What the form showed, so a save only applies what the user changed
        # (not stale values over changes the bot made after the page loaded).
        orig = {"title": rec["title"],
                **{f.name: sorted(v) if isinstance(v, set) else v for f, v in fields}}
        return render_template("record.html", tdef=tdef, rec=rec, fields=fields, pickers=pickers,
                               orig_json=json.dumps(orig, ensure_ascii=False),
                               backlinks=backlinks, source=source, history=history,
                               problems=problems or [], saved=request.args.get("saved") == "1"), status

    @app.get("/r/<ref_str>")
    def record_view(ref_str: str):
        need_login()
        tdef, rec = load_record(ref_str)
        return record_page(tdef, rec)

    @app.post("/r/<ref_str>")
    def record_save(ref_str: str):
        need_login()
        check_csrf()
        tdef, rec = load_record(ref_str)
        try:
            orig = json.loads(request.form.get("orig", ""))
            assert isinstance(orig, dict)
        except (ValueError, AssertionError):
            abort(400, "This form has expired. Go back, reload the page and try again.")
        changes, clear = diff_form(tdef, orig, request.form)
        with tx() as t:
            problems: list[str] = []
            if changes or clear:
                problems = t.update_record(g.tenant["id"], g.schema, rec["id"], changes,
                                           actor="web", by_user=True, clear=clear)
        if problems:
            _, rec = load_record(ref_str)
            return record_page(tdef, rec, problems=problems, status=400)
        return redirect(url_for("record_view", ref_str=ref_str, saved=1))

    # --- export ------------------------------------------------------------

    @app.post("/export")
    def export():
        need_login()
        check_csrf()
        with tx() as t:
            filename, data = build_export(t, g.schema, g.tenant)
            t.audit(g.tenant["id"], actor="web", action="send", target_table="none", target_id=None,
                    summary=f"export downloaded ({len(data)} bytes)")
        return Response(data, mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("message.html", title="Not found",
                               text="That page or record doesn't exist."), 404

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("message.html", title="Something's off",
                               text=getattr(e, "description", "") or "Bad request."), 400

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("message.html", title="Something failed on our side",
                               text="Please try again in a minute."), 500

    return app


# --- form <-> field values ----------------------------------------------------

def clean_text(v: str) -> str:
    """Normalise submitted text: browser newlines, NUL bytes (Postgres rejects them)."""
    return v.replace("\r\n", "\n").replace("\x00", "").strip()


def status_label(m: dict) -> str:
    """Plain words for what happened to an inbox row."""
    status, intent = m["status"], m.get("intent")
    if status in ("pending", "processing"):
        return "waiting"
    if status == "failed":
        return "failed"
    return {"log": "filed", "query": "answered", "chat": "chat", "command": "command"}.get(
        intent or "", status)


def raw_cell(column: str, value: Any) -> dict:
    """One cell of the raw table view: display text, plus whether it was cut."""
    if value is None:
        return {"text": "null", "null": True}
    if column == "embedding":
        return {"text": "[vector, 1024 numbers]"}
    if column == "search_text":
        return {"text": "[search index]"}
    if column.endswith("_hash"):
        return {"text": str(value)[:10] + "…"}
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, dt.datetime):
        text = value.isoformat(sep=" ", timespec="seconds")
    else:
        text = str(value)
    if len(text) > RAW_CELL_CHARS:
        return {"text": text[:RAW_CELL_CHARS] + "…", "cut": len(text)}
    return {"text": text}


def num_str(value: float | int) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def form_value(f: FieldDef, value: Any, links: set[str]) -> Any:
    """What the edit form shows for a stored value."""
    if f.type == "relation":
        return links
    if f.type == "multi_select":
        return set(value or [])
    if f.type == "checkbox":
        return bool(value)
    if value is None:
        return ""
    if f.type == "number" and isinstance(value, (int, float)):
        return num_str(value)
    return str(value)


def fmt_value(f: FieldDef, value: Any) -> str:
    """Short read-only rendering for list pages."""
    if value is None or value == "" or value == []:
        return ""
    if isinstance(value, list):
        return ", ".join(map(str, value))
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return num_str(value)
    return str(value)


def diff_form(tdef: TypeDef, orig: dict[str, Any], form) -> tuple[dict[str, Any], list[str]]:
    """Turn a submitted edit form into (changes, fields to clear).

    Compares each submitted value with what the form originally showed
    (`orig`), not with the record as it is now. So only fields the user
    actually changed are saved and marked user-edited, and a change the bot
    made while the page was open is not reverted by an untouched field.
    """
    changes: dict[str, Any] = {}
    clear: list[str] = []
    title = clean_text(form.get("title") or "")
    if title and title != str(orig.get("title", "")).strip():
        changes[tdef.title_field] = title
    for f in tdef.fields.values():
        if f.name not in orig:
            continue
        key = f"f:{f.name}"
        before = orig[f.name]
        if f.type == "checkbox":
            if f"present:{f.name}" not in form:
                continue
            new = key in form
            if new != bool(before):
                changes[f.name] = new
            continue
        if f.type in ("multi_select", "relation"):
            if f"present:{f.name}" not in form:
                continue
            new_set = {clean_text(v) for v in form.getlist(key) if clean_text(v)}
            if new_set == set(before or []):
                continue
            if not new_set:
                clear.append(f.name)
            elif f.type == "relation":
                changes[f.name] = sorted(new_set)
            else:
                ordered = [o for o in (*f.options, *f.deprecated) if o in new_set]
                changes[f.name] = ordered + sorted(new_set - set(ordered))
            continue
        if key not in form:
            continue
        new = clean_text(form.get(key, ""))
        old = str(before or "").strip()
        if f.type == "number" and new and old:
            try:
                if float(new) == float(old):
                    continue
            except ValueError:
                pass
        if new == old:
            continue
        if new == "":
            clear.append(f.name)
        else:
            changes[f.name] = new
    return changes, clear


def run(store: Store, settings: Settings) -> None:
    from waitress import serve
    log.info("web view on port %s (%s)", settings.web_port, settings.web_base_url or "no public URL set")
    serve(create_app(store, settings), host="0.0.0.0", port=settings.web_port, threads=4,
          ident="atya", url_scheme="https" if (settings.web_base_url or "").startswith("https") else "http")
