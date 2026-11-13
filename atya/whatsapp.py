"""WhatsApp Cloud API: client, webhook processing and sender.

Meta calls our webhook (POST /whatsapp/webhook on the web server) for every
incoming message and every delivery status. The web server checks the
signature and stores the call in whatsapp_events; the worker here processes
the queue. Incoming messages then follow the same path as Telegram: the
inbox, the processor, the outbox. A second sender thread delivers outbox rows
with channel 'whatsapp'.

WhatsApp ids are phone numbers in digits ("919711966131"). They are used as
both user id and chat id, and stored in tenants.whatsapp_phone.

Free-form messages can only be sent within 24 hours of the user's last
message. Outside that window Meta requires an approved template; until those
exist, such messages are marked failed with the reason.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import time
from typing import Any

import requests

from . import commands, documents, transcribe, wa_templates
from .config import Settings
from .receiver import reminder_button, save_as_task
from .store import Store
from .telegram import TelegramAPI, split_message

log = logging.getLogger("whatsapp")

GRAPH = "https://graph.facebook.com/v25.0"
MAX_TEXT = 4096
MAX_INTERACTIVE_BODY = 1024
MAX_BUTTONS = 3                 # reply buttons; more choices become a list
MAX_LIST_ROWS = 10
BUTTON_TITLE = 20
ROW_TITLE = 24
WINDOW = dt.timedelta(hours=24)
OUTSIDE_WINDOW = ("not sent: more than 24 hours since their last WhatsApp message "
                  "(Meta only allows template messages then)")


class WhatsAppError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


# --- client -------------------------------------------------------------------

class WhatsApp:
    """Sends messages and fetches media. Same shape as the Telegram client, so
    commands and onboarding work unchanged on either channel."""

    def __init__(self, token: str, phone_number_id: str) -> None:
        self.phone_number_id = phone_number_id
        self.http = requests.Session()
        self.http.headers["Authorization"] = f"Bearer {token}"

    def _post(self, payload: dict) -> dict:
        try:
            r = self.http.post(f"{GRAPH}/{self.phone_number_id}/messages", json=payload, timeout=30)
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            raise WhatsAppError(f"send: {e}") from e
        if r.status_code >= 400 or "error" in body:
            err = body.get("error") or {}
            detail = (err.get("error_data") or {}).get("details") or err.get("message") or r.text[:300]
            raise WhatsAppError(f"send: {err.get('code')} {detail}", err.get("code"))
        return body

    def send_message(self, chat_id: int | str, text: str, reply_to: Any = None,
                     buttons: list[list[tuple[str, str]]] | None = None,
                     preview: bool = True) -> dict:
        """Send text (split if long). Buttons go on the last part: up to three
        as reply buttons, more as a list. Returns {"message_id": wamid} of the last."""
        last = ""
        for payload in build_payloads(str(chat_id), text, buttons, preview):
            body = self._post(payload)
            last = ((body.get("messages") or [{}])[0]).get("id", "")
        return {"message_id": last}

    def send_template(self, message: dict) -> dict:
        """Send a prepared template message (see wa_templates). Returns {"message_id": wamid}."""
        body = self._post(message)
        return {"message_id": ((body.get("messages") or [{}])[0]).get("id", "")}

    def send_document(self, chat_id: int | str, filename: str, data: bytes, caption: str = "") -> dict:
        raise WhatsAppError("sending files is not supported on WhatsApp here")

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        pass

    def bot_username(self) -> str:
        return ""

    def download_file(self, media_id: str, max_bytes: int) -> bytes:
        """Fetch a file the user sent: the media id gives a short-lived URL."""
        try:
            r = self.http.get(f"{GRAPH}/{media_id}", timeout=30)
            info = r.json()
            if r.status_code >= 400 or not info.get("url"):
                raise WhatsAppError(f"media: {info.get('error', {}).get('message') or r.status_code}")
            if int(info.get("file_size") or 0) > max_bytes:
                raise WhatsAppError("file larger than the limit")
            r = self.http.get(info["url"], timeout=120, stream=True)
            r.raise_for_status()
            data = bytearray()
            for part in r.iter_content(64 * 1024):
                data += part
                if len(data) > max_bytes:
                    raise WhatsAppError("file larger than the limit")
            return bytes(data)
        except (requests.RequestException, ValueError) as e:
            raise WhatsAppError(f"media: {e}") from e


def build_payloads(to: str, text: str, buttons: list[list[tuple[str, str]]] | None,
                   preview: bool = False) -> list[dict]:
    """The API calls for one outgoing message (pure; tested directly)."""
    base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to}
    choices = [b for row in (buttons or []) for b in row][:MAX_LIST_ROWS]
    parts = split_message(text, MAX_TEXT)
    out: list[dict] = []
    tail = parts[-1] if choices else None
    if choices and len(tail) > MAX_INTERACTIVE_BODY:
        tail = None                     # too long for a button message: send it as text, then the choices
    for part in (parts[:-1] if tail is not None else parts):
        out.append({**base, "type": "text", "text": {"body": part, "preview_url": bool(preview)}})
    if choices:
        body = {"text": tail if tail is not None else "Choose:"}
        if len(choices) <= MAX_BUTTONS:
            action = {"buttons": [{"type": "reply", "reply": {"id": data[:256], "title": label[:BUTTON_TITLE]}}
                                  for label, data in choices]}
            interactive = {"type": "button", "body": body, "action": action}
        else:
            action = {"button": "Choose", "sections": [{"title": "Options", "rows": [
                {"id": data[:200], "title": label[:ROW_TITLE]} for label, data in choices]}]}
            interactive = {"type": "list", "body": body, "action": action}
        out.append({**base, "type": "interactive", "interactive": interactive})
    return out


def valid_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256: sha256=<hex HMAC of the raw body, keyed with the app secret>."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[7:], expected)


# --- webhook processing ------------------------------------------------------------

def process_event(store: Store, wa, settings: Settings, payload: dict,
                  telegram: TelegramAPI | None = None) -> list[str]:
    """One webhook call: messages and delivery statuses. Returns outcome labels."""
    outcomes: list[str] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if change.get("field") != "messages":
                continue
            ours = (value.get("metadata") or {}).get("phone_number_id")
            if ours and settings.whatsapp_phone_number_id and ours != settings.whatsapp_phone_number_id:
                outcomes.append("ignored:other-number")
                continue
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name") for c in value.get("contacts") or []}
            for msg in value.get("messages") or []:
                outcomes.append(handle_message(store, wa, settings, msg, names.get(msg.get("from")), telegram))
            for st in value.get("statuses") or []:
                outcomes.append(handle_status(store, st))
    return outcomes


def handle_status(store: Store, st: dict) -> str:
    if st.get("status") != "failed":
        return f"status:{st.get('status')}"
    err = (st.get("errors") or [{}])[0]
    code = err.get("code")
    detail = (err.get("error_data") or {}).get("details") or err.get("message") or err.get("title") or ""
    reason = OUTSIDE_WINDOW if code == 131047 else f"WhatsApp {code}: {detail}"
    with store.tx(system=True) as tx:
        found = tx.fail_outbox_by_channel_id(st.get("id") or "", reason)
    log.warning("delivery failed (%s): %s", code, detail)
    return "status:failed" if found else "status:failed-unknown"


def handle_message(store: Store, wa, settings: Settings, msg: dict, name: str | None,
                   telegram: TelegramAPI | None = None) -> str:
    phone = "".join(c for c in str(msg.get("from") or "") if c.isdigit())
    if not phone:
        return "ignored:no-sender"
    kind = msg.get("type")
    if kind in ("reaction", "system", "unsupported", "ephemeral", "request_welcome"):
        return f"ignored:{kind}"
    with store.tx(system=True) as tx:
        tenant = tx.tenant_by_whatsapp(phone)
    ctx = commands.Ctx(store=store, tg=wa, settings=settings, user_id=int(phone), chat_id=int(phone),
                       name=name, tenant=tenant, channel="whatsapp", telegram=telegram)
    captured_at = dt.datetime.fromtimestamp(int(msg.get("timestamp") or time.time()), tz=dt.timezone.utc)
    source_id = f"whatsapp:{msg.get('id')}"

    # Button and list taps carry the same data as Telegram's buttons.
    if kind in ("interactive", "button"):
        reply = (msg.get("interactive") or {}).get("button_reply") or \
                (msg.get("interactive") or {}).get("list_reply") or {}
        data = reply.get("id") or (msg.get("button") or {}).get("payload") or ""
        return handle_tap(store, ctx, data)

    text = ((msg.get("text") or {}).get("body") or "") if kind == "text" else ""
    if text.strip().startswith("/"):
        return run_command(store, ctx, text.strip(), captured_at, source_id, phone)

    if tenant is None:
        return commands.stranger(ctx)
    if tenant["status"] == "invited":
        zone = commands.match_timezone(text)
        if zone:
            commands.finish_onboarding(ctx, zone)
            return "onboarded"
        ctx.reply(commands.TZ_PROMPT, buttons=commands.tz_buttons())
        return "prompted:timezone"
    if tenant["status"] != "active":
        return "dropped:inactive-tenant"

    doc = msg.get("document") if kind == "document" else None
    file_data: bytes | None = None
    audio: dict | None = None
    filename = "file.pdf"
    if doc:
        filename = doc.get("filename") or "file.pdf"
        mime = doc.get("mime_type") or ""
        if not (mime == "application/pdf" or filename.lower().endswith(".pdf")):
            ctx.reply("I can only save PDF files for now. Other files and photos are coming later.")
            return "skipped:not-pdf"
        try:
            file_data = wa.download_file(doc["id"], documents.MAX_BYTES)
            documents.check(file_data, filename, mime)
        except documents.DocumentError as e:
            ctx.reply(str(e))
            return "skipped:bad-file"
        except WhatsAppError as e:
            if "larger than the limit" in str(e):
                ctx.reply("That file is over 20 MB. Send a smaller copy.")
                return "skipped:too-big"
            raise                                      # retried with the event
        text = doc.get("caption") or f"[PDF] {filename}"
    elif kind in ("audio", "voice"):
        a = msg.get(kind) or {}
        mime = transcribe.base_mime(a.get("mime_type") or "audio/ogg")
        if not transcribe.supported(mime):
            ctx.reply("I can't read that audio format. Voice notes, MP3 and M4A work.")
            return "skipped:audio-format"
        try:
            data = wa.download_file(a["id"], transcribe.MAX_BYTES)
        except WhatsAppError as e:
            if "larger than the limit" in str(e):
                ctx.reply(f"Voice notes can be up to {transcribe.MAX_SECONDS // 60} minutes. Send a shorter one.")
                return "skipped:too-long"
            raise
        audio = {"data": data, "mime": mime, "filename": f"voice.{transcribe.extension(mime)}"}
        text = transcribe.placeholder(None)
    elif kind in ("image", "video", "sticker"):
        ctx.reply("I can only save text, voice notes and PDF files for now. Photos and videos are coming later.")
        return "skipped:media"
    elif kind in ("location", "contacts", "order"):
        ctx.reply("I can only save text and PDF files for now.")
        return "skipped:unsupported"
    if not text.strip():
        return "skipped:no-text"

    with store.tx(tenant["id"]) as tx:
        inbox_id = tx.add_inbox(tenant["id"], source="whatsapp", raw_content=text, captured_at=captured_at,
                                source_message_id=source_id, sender=name or phone)
        if inbox_id is None:
            return "dedup"
        if file_data is not None:
            tx.add_attachment(tenant["id"], inbox_id=inbox_id, filename=filename, mime_type="application/pdf",
                              data=file_data, sha256=documents.sha256(file_data))
        if audio is not None:
            tx.add_attachment(tenant["id"], inbox_id=inbox_id, filename=audio["filename"], mime_type=audio["mime"],
                              data=audio["data"], sha256=documents.sha256(audio["data"]))
        tx.note_inbound(tenant["id"], "whatsapp")
    log.info("inbox <- %s (whatsapp, %d chars)", inbox_id, len(text))
    return "stored"


def run_command(store: Store, ctx: commands.Ctx, text: str, captured_at: dt.datetime,
                source_id: str, phone: str) -> str:
    tenant = ctx.tenant
    if tenant is not None and tenant["status"] == "active":
        with store.tx(tenant["id"]) as tx:
            ctx.inbox_id = tx.add_inbox(tenant["id"], source="whatsapp", raw_content=text, status="responded",
                                        captured_at=captured_at, source_message_id=source_id, sender=phone)
            if ctx.inbox_id is not None:
                tx.note_inbound(tenant["id"], "whatsapp")
        if ctx.inbox_id is None:
            return "dedup"
    elif tenant is not None:
        with store.tx(tenant["id"]) as tx:
            tx.note_inbound(tenant["id"], "whatsapp")
    try:
        outcome = commands.handle(ctx, text)
    except Exception:
        log.exception("command %r failed", text.split()[0])
        try:
            ctx.reply("Sorry, that failed on my side. Please try again in a minute.")
        except Exception:
            pass
        outcome = "error:command"
    if ctx.tenant is not None and ctx.inbox_id is None and outcome in ("linked", "created:invited"):
        with store.tx(ctx.tenant["id"]) as tx:      # just joined or linked: the 24 h window is open
            tx.note_inbound(ctx.tenant["id"], "whatsapp")
    if ctx.inbox_id is not None and tenant is not None:
        try:
            with store.tx(tenant["id"]) as tx:
                tx.conn.execute("UPDATE inbox SET intent = 'command', processor_notes = %s, "
                                "processed_at = now() WHERE id = %s", (outcome, ctx.inbox_id))
        except Exception:
            log.warning("could not record command outcome", exc_info=True)
    return outcome


def handle_tap(store: Store, ctx: commands.Ctx, data: str) -> str:
    tenant = ctx.tenant
    if tenant is not None:
        with store.tx(tenant["id"]) as tx:
            tx.note_inbound(tenant["id"], "whatsapp")
    if data == wa_templates.SHOW:
        return "released" if tenant is not None else "ignored:callback-no-tenant"
    if data.startswith("save:"):
        return save_as_task(store, tenant, ctx.chat_id, data[5:], channel="whatsapp")
    if data.startswith("rem:"):
        return reminder_button(store, tenant, ctx.chat_id, data, channel="whatsapp")
    if data.startswith("tz:"):
        if tenant is None or tenant["status"] not in ("invited", "active"):
            return "ignored:callback-no-tenant"
        zone = commands.match_timezone(data[3:])
        if zone is None:
            return "ignored:bad-zone"
        commands.finish_onboarding(ctx, zone)
        return "onboarded"
    return "ignored:callback"


def process_once(store: Store, wa, settings: Settings, telegram: TelegramAPI | None = None) -> int:
    with store.tx(system=True) as tx:
        events = tx.claim_whatsapp_events()
    for ev in events:
        try:
            outcomes = process_event(store, wa, settings, ev["payload"], telegram)
            log.info("event %s: %s", ev["id"], ", ".join(outcomes) or "nothing")
            error = None
        except Exception as e:
            log.exception("event %s failed (attempt %s)", ev["id"], ev["attempts"])
            error = f"{type(e).__name__}: {e}"
        with store.tx(system=True) as tx:
            tx.finish_whatsapp_event(ev["id"], error=error)
    return len(events)


def run(store: Store, wa, settings: Settings, telegram: TelegramAPI | None = None) -> None:
    log.info("whatsapp worker started")
    while True:
        try:
            if process_once(store, wa, settings, telegram) == 0:
                time.sleep(1)
        except Exception:
            log.exception("whatsapp worker loop error")
            time.sleep(5)


# --- sender ----------------------------------------------------------------------

def send_once(store: Store, wa) -> int:
    """Deliver queued WhatsApp messages. Within 24 hours of the user's last message
    anything goes; after that only templates: a reminder goes as the reminder
    template, anything else waits behind one "tap Show" template."""
    with store.tx(system=True) as tx:
        rows = tx.claim_outbox(limit=20, channel="whatsapp")
        tenants = {tid: tx.tenant(tid) for tid in {r["tenant_id"] for r in rows}}
    now = dt.datetime.now(dt.timezone.utc)
    for row in rows:
        t = tenants.get(row["tenant_id"]) or {}
        seen = t.get("whatsapp_last_inbound")
        wamid, ok, err = None, True, None
        try:
            if seen is not None and now - seen <= WINDOW:
                buttons = [[tuple(b) for b in r] for r in row["buttons"]] if row.get("buttons") else None
                wamid = wa.send_message(row["chat_id"], row["body"], buttons=buttons, preview=False).get("message_id")
            elif row.get("kind") == "reminder":
                wamid = wa.send_template(wa_templates.reminder(str(row["chat_id"]), row["body"],
                                                               row.get("buttons"))).get("message_id")
            else:
                with store.tx(system=True) as tx:
                    tx.wait_outbox(row["id"])
                    nudge = tx.nudge_due(row["tenant_id"])
                    waiting = tx._one("SELECT count(*) AS n FROM outbox WHERE tenant_id = %s AND status = 'waiting'",
                                      (row["tenant_id"],))["n"] if nudge else 0
                if nudge:
                    send_nudge(store, wa, row, t, now, waiting)
                continue                                   # stays 'waiting' until the user writes or taps Show
        except WhatsAppError as e:
            ok, err = False, str(e)
            log.warning("send failed for %s: %s", row["id"], e)
        with store.tx(system=True) as tx:
            tx.finish_outbox(row["id"], ok=ok, error=err, channel_message_id=wamid or None)
    return len(rows)


def send_nudge(store: Store, wa, row: dict, tenant: dict, now: dt.datetime, waiting: int) -> None:
    """The "tap Show" template for messages held outside the 24-hour window: the
    Utility-worded v2 template, or the older one if v2 isn't approved yet."""
    from zoneinfo import ZoneInfo
    from .reminders import human
    from .schema import CLOSED_STATUSES
    to = str(row["chat_id"])
    tz = ZoneInfo(tenant.get("timezone") or "UTC")
    local = now.astimezone(tz)
    if row.get("kind") == "brief":
        today = local.date().isoformat()
        with store.tx(row["tenant_id"]) as tx:
            due = tx.due_records(row["tenant_id"], "task", until=local.date().isoformat(), closed=CLOSED_STATUSES,
                                 limit=500)
        on_day = sum(1 for r in due if str(r["fields"].get("Due Date", ""))[:10] == today)
        label = f"{local:%A} {local.day} {local:%B}"
        first = wa_templates.brief_summary(to, label, on_day, len(due) - on_day)
        fallback = wa_templates.brief_ready(to, label)
    else:
        when = row["created_at"]
        if row.get("inbox_id"):
            with store.tx(row["tenant_id"]) as tx:
                src = tx._one("SELECT captured_at FROM inbox WHERE id = %s", (row["inbox_id"],))
            when = src["captured_at"] if src else when
        hhmm, ampm, day = human(when, local).split(" ", 2)       # "6:40 pm today" -> "today at 6:40 pm"
        first = wa_templates.reply_waiting(to, f"{day} at {hhmm} {ampm}")
        fallback = wa_templates.update_waiting(to, waiting)
    try:
        wa.send_template(first)
    except WhatsAppError as e:
        if getattr(e, "code", None) not in wa_templates.NOT_USABLE_CODES:
            raise
        log.info("template %s not usable yet (%s); sending the older one", first["template"]["name"], e.code)
        wa.send_template(fallback)


def run_sender(store: Store, wa, settings: Settings) -> None:
    log.info("whatsapp sender started")
    while True:
        try:
            if send_once(store, wa) == 0:
                time.sleep(1)
        except Exception:
            log.exception("whatsapp sender loop error")
            time.sleep(settings.poll_interval)
