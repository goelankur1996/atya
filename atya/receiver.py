"""Worker 1: Telegram -> inbox. No AI here, on purpose.

Stores each message verbatim with its channel metadata and nothing else, so
capture keeps working even when the AI step is down.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

from . import commands, documents, reminders, transcribe
from .config import Settings
from .schema import CLOSED_STATUSES, load_schema
from .store import Store
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger("receiver")

OFFSET_KEY = "telegram.offset"
ERROR_BACKOFF_SECONDS = 5


def handle_update(store: Store, tg: TelegramAPI, settings: Settings, update: dict) -> str:
    """Process one Telegram update. Returns a short outcome label (for logs/tests)."""
    if cq := update.get("callback_query"):
        return handle_callback(store, tg, settings, cq)
    msg = update.get("message")
    if not msg:
        return "ignored:not-a-message"
    sender = msg.get("from") or {}
    user_id = sender.get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text") or msg.get("caption") or ""
    if not user_id or not chat_id:
        return "ignored:no-sender"

    with store.tx(system=True) as tx:
        tenant = tx.tenant_by_telegram(user_id)
    ctx = commands.Ctx(store=store, tg=tg, settings=settings, user_id=user_id, chat_id=chat_id,
                       name=sender.get("first_name") or sender.get("username"), tenant=tenant)

    if text.startswith("/"):
        if tenant is not None and tenant["status"] == "active":
            # Keep the command in the inbox (already answered) so the
            # conversation can be reviewed; replies are logged against it.
            with store.tx(tenant["id"]) as tx:
                ctx.inbox_id = tx.add_inbox(
                    tenant["id"], source="telegram", raw_content=text, status="responded",
                    captured_at=dt.datetime.fromtimestamp(msg.get("date") or time.time(), tz=dt.timezone.utc),
                    source_message_id=f"telegram:{chat_id}:{msg.get('message_id')}",
                    sender=sender.get("username") or str(user_id))
            if ctx.inbox_id is None:
                return "dedup"
        try:
            outcome = commands.handle(ctx, text)
        except Exception:
            # Commands are not retried (a retry could repeat an export or an invite).
            log.exception("command %r failed", text.split()[0])
            try:
                ctx.reply("Sorry, that failed on my side. Please try again in a minute.")
            except Exception:
                pass
            outcome = "error:command"
        if ctx.inbox_id is not None:
            try:
                with store.tx(tenant["id"]) as tx:
                    tx.conn.execute("UPDATE inbox SET intent = 'command', processor_notes = %s, "
                                    "processed_at = now() WHERE id = %s", (outcome, ctx.inbox_id))
            except Exception:
                log.warning("could not record command outcome", exc_info=True)
        return outcome

    if tenant is None:
        if ctx.is_owner:  # the owner may start by just talking to the bot
            with store.tx(system=True) as tx:
                tenant = tx.create_tenant(telegram_user_id=user_id, telegram_chat_id=chat_id,
                                          display_name=ctx.name, timezone=settings.owner_timezone,
                                          actor="receiver")
            log.info("created owner tenant %s", tenant["id"])
        else:
            return commands.stranger(ctx)
    if tenant["status"] == "invited":
        zone = commands.match_timezone(text)
        ctx.tenant = tenant
        if zone:
            commands.finish_onboarding(ctx, zone)
            return "onboarded"
        ctx.reply(commands.TZ_PROMPT, buttons=commands.tz_buttons())
        return "prompted:timezone"
    if tenant["status"] != "active":
        return "dropped:inactive-tenant"
    doc = msg.get("document")
    voice = msg.get("voice") or msg.get("audio")
    file_data: bytes | None = None
    audio: dict | None = None            # {"data", "mime", "seconds", "filename"} for a voice note
    if voice:
        mime = voice.get("mime_type") or ("audio/ogg" if msg.get("voice") else "")
        seconds = voice.get("duration")
        if not transcribe.supported(mime):
            ctx.reply("I can't read that audio format. Voice notes, MP3 and M4A work.")
            return "skipped:audio-format"
        if (voice.get("file_size") or 0) > transcribe.MAX_BYTES or (seconds or 0) > transcribe.MAX_SECONDS:
            ctx.reply(f"Voice notes can be up to {transcribe.MAX_SECONDS // 60} minutes. Send a shorter one.")
            return "skipped:too-long"
        data = tg.download_file(voice["file_id"], transcribe.MAX_BYTES)   # failure: update is retried
        audio = {"data": data, "mime": transcribe.base_mime(mime), "seconds": seconds,
                 "filename": voice.get("file_name") or f"voice.{transcribe.extension(mime)}"}
        text = transcribe.placeholder(seconds)
    elif doc:
        name = doc.get("file_name") or "file.pdf"
        if not (doc.get("mime_type") == "application/pdf" or name.lower().endswith(".pdf")):
            ctx.reply("I can only save PDF files for now. Other files and photos are coming later.")
            return "skipped:not-pdf"
        if (doc.get("file_size") or 0) > documents.MAX_BYTES:
            ctx.reply("That file is over 20 MB, the most a bot can receive. Send a smaller copy.")
            return "skipped:too-big"
        try:
            file_data = tg.download_file(doc["file_id"], documents.MAX_BYTES)
            documents.check(file_data, name, doc.get("mime_type") or "")
        except documents.DocumentError as e:
            ctx.reply(str(e))
            return "skipped:bad-file"
        # A download failure (TelegramError) propagates: the update is retried.
        if not text.strip():
            text = f"[PDF] {name}"          # the message is the file itself
    elif msg.get("photo") or msg.get("video") or msg.get("video_note"):
        ctx.reply("I can only save text, voice notes and PDF files for now. Photos and videos are coming later.")
        return "skipped:media"
    if not text.strip():
        return "skipped:no-text"

    captured_at = dt.datetime.fromtimestamp(msg.get("date") or time.time(), tz=dt.timezone.utc)
    with store.tx(tenant["id"]) as tx:
        inbox_id = tx.add_inbox(
            tenant["id"], source="telegram", raw_content=text, captured_at=captured_at,
            source_message_id=f"telegram:{chat_id}:{msg.get('message_id')}",
            sender=sender.get("username") or str(user_id))
        if inbox_id is None:
            return "dedup"
        if doc and file_data is not None:
            tx.add_attachment(tenant["id"], inbox_id=inbox_id, filename=doc.get("file_name") or "file.pdf",
                              mime_type="application/pdf", data=file_data,
                              sha256=documents.sha256(file_data))
        if audio is not None:
            tx.add_attachment(tenant["id"], inbox_id=inbox_id, filename=audio["filename"], mime_type=audio["mime"],
                              data=audio["data"], sha256=documents.sha256(audio["data"]), seconds=audio["seconds"])
        tx.note_inbound(tenant["id"], "telegram")
        uname = (sender.get("username") or "").lower() or None
        if tenant.get("telegram_username") != uname:          # for signing in on the web by @username
            tx.conn.execute("UPDATE tenants SET telegram_username = %s WHERE id = %s", (uname, tenant["id"]))
        if tenant["telegram_chat_id"] != chat_id:
            tx.conn.execute("UPDATE tenants SET telegram_chat_id = %s WHERE id = %s",
                            (chat_id, tenant["id"]))
    log.info("inbox <- %s (%d chars)", inbox_id, len(text))
    return "stored"


def handle_callback(store: Store, tg: TelegramAPI, settings: Settings, cq: dict) -> str:
    """Button taps. Only the timezone picker uses them for now."""
    user_id = (cq.get("from") or {}).get("id")
    chat_id = ((cq.get("message") or {}).get("chat") or {}).get("id") or user_id
    data = cq.get("data") or ""
    try:
        tg.answer_callback(cq["id"])
    except Exception:
        log.warning("answerCallbackQuery failed", exc_info=True)
    with store.tx(system=True) as tx:
        tenant = tx.tenant_by_telegram(user_id)
    if data.startswith("save:"):
        return save_as_task(store, tenant, chat_id, data[5:], channel="telegram")
    if data.startswith("rem:"):
        return reminder_button(store, tenant, chat_id, data, channel="telegram")
    if not data.startswith("tz:"):
        return "ignored:callback"
    if tenant is None or tenant["status"] not in ("invited", "active"):
        return "ignored:callback-no-tenant"
    zone = commands.match_timezone(data[3:])
    if zone is None:
        return "ignored:bad-zone"
    ctx = commands.Ctx(store=store, tg=tg, settings=settings, user_id=user_id, chat_id=chat_id,
                       name=None, tenant=tenant)
    commands.finish_onboarding(ctx, zone)
    return "onboarded"


def save_as_task(store: Store, tenant: dict | None, chat_id: int, inbox_id: str, *, channel: str) -> str:
    """'Save as task' tapped under an answer that found nothing."""
    try:
        iid = uuid.UUID(inbox_id)
    except ValueError:
        return "ignored:bad-save"
    if tenant is None or tenant["status"] != "active":
        return "ignored:callback-no-tenant"
    with store.tx(tenant["id"]) as tx:              # row-level security: only their own message
        if not tx.refile_as_capture(tenant["id"], iid):
            return "ignored:already-saved"
        tx.add_outbox(tenant["id"], chat_id=chat_id, body="Saving it as a task…", inbox_id=iid,
                      actor="receiver", channel=channel)
    return "refiled"


def reminder_button(store: Store, tenant: dict | None, chat_id: int, data: str, *, channel: str) -> str:
    """Done / In 1 hour / Tomorrow 9am under a reminder."""
    _, choice, rid = (data.split(":", 2) + ["", ""])[:3]
    try:
        record_id = uuid.UUID(rid)
    except ValueError:
        return "ignored:bad-reminder"
    if choice not in ("done", "1h", "tmrw"):
        return "ignored:bad-reminder"
    if tenant is None or tenant["status"] != "active":
        return "ignored:callback-no-tenant"
    schema = load_schema(tenant["schema_name"])
    tz = reminders.tzinfo(tenant["timezone"])
    with store.tx(tenant["id"]) as tx:                 # row-level security: own tasks only
        rec = tx.record(tenant["id"], record_id)
        if rec is None or rec["type"] != "task":
            return "ignored:no-task"
        ref = f"{schema.types['task'].prefix}-{rec['seq']}"
        if choice == "done":
            if rec["fields"].get("Status") in CLOSED_STATUSES:
                return "ignored:already-done"
            changes = {"Status": "done"}
            if "Completed At" in schema.types["task"].fields:
                changes["Completed At"] = dt.datetime.now(tz).date().isoformat()
            tx.update_record(tenant["id"], schema, record_id, changes, actor="receiver", by_user=True)
            body = f"Done: {rec['title']} ({ref})"
        else:
            new_at = reminders.snooze_value(choice, tz)
            tx.update_record(tenant["id"], schema, record_id, {"Remind At": new_at}, actor="receiver",
                             by_user=True)
            at = reminders.parse_at(new_at, tz)
            body = f"OK, I'll remind you again at {reminders.human(at, dt.datetime.now(tz))}: {rec['title']} ({ref})"
        tx.add_outbox(tenant["id"], chat_id=chat_id, body=body, actor="receiver", channel=channel)
    return f"reminder:{choice}"


def poll_once(store: Store, tg: TelegramAPI, settings: Settings, *, timeout: int = 30) -> int:
    """One long-poll round. The offset is saved only after updates are stored."""
    with store.tx(system=True) as tx:
        offset = tx.get_state(OFFSET_KEY)
    updates = tg.get_updates(offset, timeout=timeout)
    for upd in updates:
        try:
            handle_update(store, tg, settings, upd)
        except Exception:
            # Leave the offset before this update so it is retried next round.
            log.exception("failed to store update %s", upd.get("update_id"))
            raise
        with store.tx(system=True) as tx:
            tx.set_state(OFFSET_KEY, upd["update_id"] + 1)
    return len(updates)


def run(store: Store, tg: TelegramAPI, settings: Settings) -> None:
    log.info("receiver started")
    while True:
        try:
            poll_once(store, tg, settings)
        except TelegramError as e:
            log.warning("telegram: %s", e)
            time.sleep(ERROR_BACKOFF_SECONDS)
        except Exception:
            log.exception("receiver loop error")
            time.sleep(ERROR_BACKOFF_SECONDS)
