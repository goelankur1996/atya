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
import uuid
import zoneinfo
from typing import Any

from flask import Flask, Response, abort, g, redirect, render_template, request, url_for

from ..config import Settings
from .. import documents, whatsapp
from ..export import build_export
from ..schema import CLOSED_STATUSES, FieldDef, TypeDef, load_schema
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
    "attachments": ("tenant_id", "created_at DESC", "Files you sent (PDFs), with the text read from them."),
    "attachment_chunks": ("tenant_id", "attachment_id, seq", "Your files' text in searchable passages."),
}
RAW_CELL_CHARS = 600
# Columns to show instead of SELECT * (never load file bytes or vectors in bulk).
RAW_SELECT = {
    "attachments": ("id, tenant_id, inbox_id, record_id, filename, mime_type, bytes, pages, extraction, "
                    "sha256, left(text, 700) AS text, created_at"),
    "records": "id, tenant_id, type, seq, title, body, fields, user_edited_fields, source_inbox_id, "
               "external_id, created_at, updated_at, embedded_at, (embedding IS NOT NULL) AS has_embedding",
}
LIST_COLUMNS = ("Status", "Due Date", "Date", "Priority", "Area", "Relationship", "Type", "Mood")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_app(store: Store, settings: Settings) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = documents.MAX_BYTES + 1024 * 1024   # a PDF plus the form
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
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; "
            "form-action 'self'; connect-src 'self'; "
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
        return {"ref": ref, "fmt_time": fmt_time, "fmt_clock": fmt_clock, "fmt_day": fmt_day,
                "fmt_cell": fmt_cell,
                "fmt_value": fmt_value, "status_label": status_label,
                "csrf": g.get("csrf", ""),
                "tenant": g.get("tenant"), "schema": g.get("schema")}

    def ref(rec: dict) -> str:
        tdef = g.schema.types.get(rec["type"])
        return f"{tdef.prefix if tdef else rec['type'].upper()}-{rec['seq']}"

    def user_tz() -> dt.tzinfo:
        try:
            return zoneinfo.ZoneInfo(g.tenant["timezone"])
        except Exception:
            return dt.timezone.utc

    def local_now() -> dt.datetime:
        return dt.datetime.now(user_tz())

    def fmt_time(ts: dt.datetime | None) -> str:
        if ts is None:
            return ""
        local = ts.astimezone(user_tz())
        today = local_now().date()
        if local.date() == today:
            return local.strftime("Today, %H:%M")
        if local.date() == today - dt.timedelta(days=1):
            return local.strftime("Yesterday, %H:%M")
        if local.year == today.year:
            return local.strftime("%a %-d %b, %H:%M")
        return local.strftime("%-d %b %Y, %H:%M")

    def fmt_cell(f: FieldDef, value: Any) -> str:
        """List-page rendering: dates as 'Fri' / '13 Nov', the rest via fmt_value."""
        if f.type == "date" and isinstance(value, str) and len(value) >= 10:
            label = day_label(value[:10], local_now().date())
            if len(value) >= 16 and value[10] == "T":
                h, m = int(value[11:13]), int(value[14:16])
                label += f", {h % 12 or 12}:{m:02d} {'am' if h < 12 else 'pm'}"
            return label
        return fmt_value(f, value).replace("_", " ")

    def fmt_clock(ts: dt.datetime | None) -> str:
        return ts.astimezone(user_tz()).strftime("%H:%M") if ts else ""

    def fmt_day(ts: dt.datetime | None) -> str:
        if ts is None:
            return ""
        return day_label(ts.astimezone(user_tz()).date().isoformat(), local_now().date(), long=True)

    # --- sign in / out -----------------------------------------------------

    @app.get("/")
    def home():
        if g.tenant is None:
            return render_template("signed_out.html")
        tid = g.tenant["id"]
        now = local_now()
        due: list[dict] = []
        with tx() as t:
            counts = t.count_by_type(tid)
            if "task" in g.schema.types:
                until = (now.date() + dt.timedelta(days=6)).isoformat()
                due = t.due_records(tid, "task", until=until, closed=CLOSED_STATUSES)
                for d in due:
                    d["linked"] = [l["title"] for l in t.linked_titles(d["id"])][:2]
            recent = t.messages_page(tid, limit=5)
            filed: dict = {}
            for r in t.records_for([m["id"] for m in recent]):
                filed.setdefault(r["source_inbox_id"], []).append(r)
        today = now.date().isoformat()
        for d in due:
            day = str(d["fields"].get("Due Date", ""))[:10]
            d["due_label"] = day_label(day, now.date())
            d["overdue"] = day < today
            d["today"] = day == today
        main_types = [(td, counts.get(td.key, 0)) for td in g.schema.types.values() if not td.child]
        trackers = [(td, counts.get(td.key, 0)) for td in g.schema.types.values() if td.child]
        return render_template("home.html", main_types=main_types, trackers=trackers, due=due,
                               recent=recent, filed=filed, greeting=greeting(now), closed=CLOSED_STATUSES,
                               today_label=now.strftime("%A, %-d %B"))

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

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html", email=settings.privacy_email)

    # --- WhatsApp webhook (Meta calls this; no session, no CSRF: the signature is the check) ---

    @app.get("/whatsapp/webhook")
    def whatsapp_verify():
        if not settings.whatsapp_on:
            abort(404)
        token = request.args.get("hub.verify_token", "")
        challenge = request.args.get("hub.challenge", "")
        if (request.args.get("hub.mode") == "subscribe" and challenge.isalnum()
                and hmac.compare_digest(token.encode(), settings.whatsapp_verify_token.encode())):
            return Response(challenge, mimetype="text/plain")
        abort(403)

    @app.post("/whatsapp/webhook")
    def whatsapp_webhook():
        if not settings.whatsapp_on:
            abort(404)
        body = request.get_data(cache=False)
        if not whatsapp.valid_signature(settings.whatsapp_app_secret, body,
                                        request.headers.get("X-Hub-Signature-256")):
            log.warning("whatsapp webhook: bad signature")
            abort(403)
        try:
            payload = json.loads(body)
        except ValueError:
            abort(400)
        if not isinstance(payload, dict):
            abort(400)
        with store.tx(system=True) as tx:
            tx.add_whatsapp_event(payload)
        return Response("", status=200)

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
        status_field = tdef.fields.get("Status")
        statuses = list(status_field.options) if status_field and status_field.type == "select" else []
        show = request.args.get("show", "")
        if show not in ("", "open", *statuses):
            show = ""
        q = clean_text(request.args.get("q", ""))[:200]
        with tx() as t:
            rows = t.search_records(g.tenant["id"], q, type_key=type_key,
                                    status=show if show in statuses else None,
                                    open_only=show == "open", closed=CLOSED_STATUSES,
                                    limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE,
                                    order="recent")
        cols = [c for c in LIST_COLUMNS if c in tdef.fields and c != "Status"][:2]
        return render_template("list.html", tdef=tdef, rows=rows[:PAGE_SIZE], cols=cols, page=page,
                               more=len(rows) > PAGE_SIZE, statuses=statuses, show=show, q=q,
                               deleted=request.args.get("deleted", "")[:20],
                               closed=CLOSED_STATUSES)

    @app.route("/t/<type_key>/new", methods=["GET", "POST"])
    def record_new(type_key: str):
        need_login()
        tdef = g.schema.types.get(type_key) or abort(404)
        problems: list[str] = []
        values: dict[str, Any] = {}
        if request.method == "POST":
            check_csrf()
            title = clean_text(request.form.get("title", ""))[:500]
            values = new_form_values(tdef, request.form)
            body = clean_text(request.form.get("body", "")) if not tdef.verbatim_body else ""
            if not title:
                problems = [f"{tdef.title_field} is required."]
            else:
                with tx() as t:
                    rec, dropped = t.create_record(g.tenant["id"], g.schema, type_key, title=title,
                                                   fields=values, body=body or None, actor="web",
                                                   by_user=True, allow_deprecated=False)
                ref_str = f"{tdef.prefix}-{rec['seq']}"
                if dropped:
                    with tx() as t:
                        rec = t.record(g.tenant["id"], rec["id"])
                    return record_page(tdef, rec, problems=dropped, status=400)
                return redirect(url_for("record_view", ref_str=ref_str, saved=1))
        with tx() as t:
            pickers = {name: t.titles_of_type(g.tenant["id"], f.to)
                       for name, f in tdef.relation_fields.items() if f.to}
        fields = [(f, form_value(f, values.get(f.name, f.default if f.type != "relation" else None),
                                 set(values.get(f.name) or []) if f.type == "relation" else set()))
                  for f in tdef.fields.values()]
        return render_template("record_new.html", tdef=tdef, fields=fields, pickers=pickers,
                               problems=problems, title=request.form.get("title", "")), \
            (400 if problems else 200)

    @app.get("/search")
    def search():
        need_login()
        q = clean_text(request.args.get("q", ""))[:500]
        type_key = request.args.get("type", "")
        if type_key not in g.schema.types:
            type_key = ""
        open_only = request.args.get("open") == "1"
        rows: list[dict] = []
        msg_count = 0
        if q:
            with tx() as t:
                rows = t.search_records(g.tenant["id"], q, type_key=type_key or None,
                                        open_only=open_only, closed=CLOSED_STATUSES, limit=PAGE_SIZE)
                msg_count = len(t.messages_page(g.tenant["id"], query=q, limit=100))
        return render_template("search.html", q=q, rows=rows, type_key=type_key, open_only=open_only,
                               msg_count=msg_count, top_q=q)

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
            files: dict = {}
            for a in t.attachments_for(ids):
                files.setdefault(a["inbox_id"], []).append(a)
            # Bot messages with no message of yours behind them (e.g. imported
            # from v1), placed in time order on the unfiltered view.
            extra: list[dict] = []
            if kind == "all" and not q:
                newest = prev_row["captured_at"] if prev_row else None
                oldest = rows[-1]["captured_at"] if rows and more else None
                extra = t.unlinked_outbox(tid, after=oldest, before=newest)
        items = [{"kind": "in", "at": r["captured_at"], "row": r, "files": files.get(r["id"], []),
                  "replies": replies.get(r["id"], []), "filed": filed.get(r["id"], [])} for r in rows]
        items += [{"kind": "out", "at": o["created_at"], "row": o} for o in extra]
        # Read like a chat: oldest at the top of the page, newest at the bottom.
        # (Page 1 is still the newest 50; "Older" pages go back in time.)
        items.sort(key=lambda i: i["at"])
        last_day = None
        for it in items:
            day = fmt_day(it["at"])
            it["day"] = day if day != last_day else None
            last_day = day
        pending_ids = [str(it["row"]["id"]) for it in items
                       if it["kind"] == "in" and it["row"]["status"] in ("pending", "processing")]
        return render_template("messages.html", items=items, kind=kind, kinds=MESSAGE_KINDS, q=q,
                               page=page, more=more, pending_ids=pending_ids,
                               note=request.args.get("note", ""), why=request.args.get("why", "")[:200])

    @app.post("/messages/send")
    def message_send():
        """Chat from the web: the message joins the same pipeline as Telegram."""
        need_login()
        check_csrf()
        text = request.form.get("text", "").replace("\r\n", "\n").replace("\x00", "").strip()[:8000]
        upload = request.files.get("file")
        data = upload.read(documents.MAX_BYTES + 1) if upload and upload.filename else b""
        name = clean_text(upload.filename)[:200] if data else ""
        if data:
            try:
                documents.check(data, name, upload.mimetype or "")
            except documents.DocumentError as e:
                return redirect(url_for("messages", note="file", why=str(e)) + "#latest")
        if not text and not data:
            return redirect(url_for("messages") + "#latest")
        if text.startswith("/") and not data:
            return redirect(url_for("messages", note="commands") + "#latest")
        with tx() as t:
            iid = t.add_inbox(g.tenant["id"], source="web", raw_content=text or f"[PDF] {name}",
                              source_message_id=f"web:{uuid.uuid4()}", sender="web", actor="web")
            if data:
                t.add_attachment(g.tenant["id"], inbox_id=iid, filename=name or "file.pdf",
                                 mime_type="application/pdf", data=data, sha256=documents.sha256(data),
                                 actor="web")
        return redirect(url_for("messages") + "#latest")

    @app.get("/files/<attachment_id>")
    def file_download(attachment_id: str):
        """Download one of the user's files (their own only: row-level security)."""
        need_login()
        try:
            aid = uuid.UUID(attachment_id)
        except ValueError:
            abort(404)
        with tx() as t:
            a = t.attachment(g.tenant["id"], aid, with_data=True)
        if a is None or a["data"] is None:
            abort(404)
        safe = re.sub(r'[^A-Za-z0-9._ -]', "_", a["filename"] or "file.pdf")[:120]
        resp = Response(bytes(a["data"]), mimetype=a["mime_type"] or "application/pdf")
        resp.headers["Content-Disposition"] = f'attachment; filename="{safe}"'
        return resp

    @app.get("/messages/pending")
    def messages_pending():
        """Polled by the page while a message is being processed."""
        need_login()
        ids = []
        for part in request.args.get("ids", "").split(",")[:50]:
            try:
                ids.append(uuid.UUID(part))
            except ValueError:
                continue
        with tx() as t:
            still = t.pending_inbox(g.tenant["id"], ids) if ids else []
        return {"pending": [str(i) for i in still]}

    @app.post("/messages/<inbox_id>/save")
    def message_save_as_task(inbox_id: str):
        """'Save as task' under a reply that found nothing (same as the Telegram button)."""
        need_login()
        check_csrf()
        try:
            iid = uuid.UUID(inbox_id)
        except ValueError:
            abort(404)
        with tx() as t:
            t.refile_as_capture(g.tenant["id"], iid)
        return redirect(url_for("messages") + "#latest")

    @app.post("/messages/<inbox_id>/retry")
    def message_retry(inbox_id: str):
        need_login()
        check_csrf()
        try:
            iid = uuid.UUID(inbox_id)
        except ValueError:
            abort(404)
        with tx() as t:
            t.retry_inbox(g.tenant["id"], iid)
        return redirect(url_for("messages", kind=request.form.get("kind") or None))

    # --- model tests (owner only) ---------------------------------------------

    def need_owner():
        need_login()
        if not settings.owner_telegram_user_id or g.tenant.get("telegram_user_id") != settings.owner_telegram_user_id:
            abort(404)

    @app.get("/eval")
    def eval_index():
        need_owner()
        with store.tx(system=True) as t:
            runs = t.get_state("eval:index") or []
        return render_template("eval_index.html", runs=runs)

    @app.get("/eval/<run_id>")
    def eval_report(run_id: str):
        need_owner()
        if not re.fullmatch(r"\d{8}-\d{6}(-[0-9a-f]{4})?", run_id):
            abort(404)
        with store.tx(system=True) as t:
            r = t.get_state(f"eval:{run_id}")
        if not r:
            abort(404)
        return render_template("eval_report.html", r=r)

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
            select = RAW_SELECT.get(name, "*")
            cur = t.conn.execute(
                f"SELECT {select} FROM {name} WHERE {col} = %s ORDER BY {order} LIMIT %s OFFSET %s",  # noqa: S608
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
            files = t.attachments_of_record(rec["id"])
            source = t.inbox_row(rec["source_inbox_id"]) if rec["source_inbox_id"] else None
            history = t.audit_for(g.tenant["id"], rec["id"])
        fields = [(f, form_value(f, rec["fields"].get(f.name), links.get(f.name, set())))
                  for f in tdef.fields.values()]
        # What the form showed, so a save only applies what the user changed
        # (not stale values over changes the bot made after the page loaded).
        orig = {"title": rec["title"], "body": rec["body"] or "",
                **{f.name: sorted(v) if isinstance(v, set) else v for f, v in fields}}
        return render_template("record.html", tdef=tdef, rec=rec, fields=fields, pickers=pickers,
                               orig_json=json.dumps(orig, ensure_ascii=False),
                               backlinks=backlinks, source=source, history=history, files=files,
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
        new_body = None
        if not tdef.verbatim_body and "body" in request.form:
            candidate = clean_text(request.form.get("body", ""))
            if candidate != str(orig.get("body", "")).strip():
                new_body = candidate
        with tx() as t:
            problems: list[str] = []
            if changes or clear:
                problems = t.update_record(g.tenant["id"], g.schema, rec["id"], changes,
                                           actor="web", by_user=True, clear=clear)
            if new_body is not None:
                t.update_body(g.tenant["id"], g.schema, rec["id"], new_body)
        if problems:
            _, rec = load_record(ref_str)
            return record_page(tdef, rec, problems=problems, status=400)
        return redirect(url_for("record_view", ref_str=ref_str, saved=1))

    @app.post("/r/<ref_str>/quick")
    def record_quick(ref_str: str):
        """One-field change from a list (mark done, change status)."""
        need_login()
        check_csrf()
        tdef, rec = load_record(ref_str)
        field = request.form.get("field", "")
        value = clean_text(request.form.get("value", ""))
        fdef = tdef.fields.get(field)
        if fdef is None or fdef.type in ("relation", "multi_select"):
            abort(400, "That field can't be changed from here.")
        changes: dict[str, Any] = {}
        clear: list[str] = []
        if value:
            changes[field] = value
        else:
            clear.append(field)
        today = local_now().date().isoformat()
        if field == "Status" and value in CLOSED_STATUSES[:2] and "Completed At" in tdef.fields \
                and not rec["fields"].get("Completed At"):
            changes["Completed At"] = today
        with tx() as t:
            problems = t.update_record(g.tenant["id"], g.schema, rec["id"], changes,
                                       actor="web", by_user=True, clear=clear)
        if problems:
            abort(400, "; ".join(problems))
        return redirect(safe_back(request.form.get("back")) or url_for("record_view", ref_str=ref_str))

    @app.route("/r/<ref_str>/delete", methods=["GET", "POST"])
    def record_delete(ref_str: str):
        need_login()
        tdef, rec = load_record(ref_str)
        if request.method == "GET":
            return render_template("confirm_delete.html", tdef=tdef, rec=rec)
        check_csrf()
        with tx() as t:
            gone = t.delete_record(g.tenant["id"], g.schema, rec["id"])
        return redirect(url_for("type_list", type_key=tdef.key, deleted=gone))

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

def safe_back(target: str | None) -> str | None:
    """Only same-site relative paths, so a form can't redirect elsewhere."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


def new_form_values(tdef: TypeDef, form) -> dict[str, Any]:
    """Field values from the New record form (empty fields left out)."""
    values: dict[str, Any] = {}
    for f in tdef.fields.values():
        key = f"f:{f.name}"
        if f.type == "checkbox":
            if key in form:
                values[f.name] = True
        elif f.type in ("multi_select", "relation"):
            picked = [clean_text(v) for v in form.getlist(key) if clean_text(v)]
            if picked:
                values[f.name] = picked
        else:
            v = clean_text(form.get(key, ""))
            if v:
                values[f.name] = v
    return values


def clean_text(v: str) -> str:
    """Normalise submitted text: browser newlines, NUL bytes (Postgres rejects them)."""
    return v.replace("\r\n", "\n").replace("\x00", "").strip()


def greeting(now: dt.datetime) -> str:
    h = now.hour
    return "Good morning" if 5 <= h < 12 else "Good afternoon" if 12 <= h < 17 else "Good evening"


def day_label(iso_day: str, today: dt.date, *, long: bool = False) -> str:
    """'Today', 'Tomorrow', 'Yesterday', a weekday this week, else '11 Nov'."""
    try:
        d = dt.date.fromisoformat(iso_day[:10])
    except ValueError:
        return iso_day
    delta = (d - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta == -1:
        return "Yesterday"
    if long:
        return d.strftime("%A, %-d %B") if d.year == today.year else d.strftime("%-d %B %Y")
    if 1 < delta < 7:
        return d.strftime("%a")
    return d.strftime("%-d %b") if d.year == today.year else d.strftime("%-d %b %Y")


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
    if column in ("search_text", "search"):
        return {"text": "[search index]"}
    if column == "data" and isinstance(value, (bytes, memoryview)):
        return {"text": f"[file, {len(value) // 1024} KB]"}
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
    if f.type == "date" and "T" in str(value):
        return str(value)[:16]                     # what a datetime-local input shows
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
