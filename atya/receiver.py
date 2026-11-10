"""Worker 1: Telegram -> inbox. No AI here, on purpose.

Stores each message verbatim with its channel metadata and nothing else, so
capture keeps working even when the AI step is down.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import uuid

from . import commands
from .config import Settings
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
    if not text.strip():
        # Photos/voice/files without text: stored as attachments in a later milestone.
        return "skipped:no-text"

    captured_at = dt.datetime.fromtimestamp(msg.get("date") or time.time(), tz=dt.timezone.utc)
    with store.tx(tenant["id"]) as tx:
        inbox_id = tx.add_inbox(
            tenant["id"], source="telegram", raw_content=text, captured_at=captured_at,
            source_message_id=f"telegram:{chat_id}:{msg.get('message_id')}",
            sender=sender.get("username") or str(user_id))
        if inbox_id is None:
            return "dedup"
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
    if data.startswith("save:"):
        return save_as_task(store, tg, user_id, chat_id, data[5:])
    if not data.startswith("tz:"):
        return "ignored:callback"
    with store.tx(system=True) as tx:
        tenant = tx.tenant_by_telegram(user_id)
    if tenant is None or tenant["status"] not in ("invited", "active"):
        return "ignored:callback-no-tenant"
    zone = commands.match_timezone(data[3:])
    if zone is None:
        return "ignored:bad-zone"
    ctx = commands.Ctx(store=store, tg=tg, settings=settings, user_id=user_id, chat_id=chat_id,
                       name=None, tenant=tenant)
    commands.finish_onboarding(ctx, zone)
    return "onboarded"


def save_as_task(store: Store, tg: TelegramAPI, user_id: int, chat_id: int, inbox_id: str) -> str:
    """'Save as task' tapped under an answer that found nothing."""
    try:
        iid = uuid.UUID(inbox_id)
    except ValueError:
        return "ignored:bad-save"
    with store.tx(system=True) as tx:
        tenant = tx.tenant_by_telegram(user_id)
    if tenant is None or tenant["status"] != "active":
        return "ignored:callback-no-tenant"
    with store.tx(tenant["id"]) as tx:              # row-level security: only their own message
        if not tx.refile_as_capture(tenant["id"], iid):
            return "ignored:already-saved"
        tx.add_outbox(tenant["id"], chat_id=chat_id, body="Saving it as a task…", inbox_id=iid,
                      actor="receiver")
    return "refiled"


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
