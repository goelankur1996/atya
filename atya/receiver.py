"""Worker 1: Telegram -> inbox. No AI here, on purpose.

Stores each message verbatim with its channel metadata and nothing else, so
capture keeps working even when the AI step is down.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from .config import Settings
from .store import Store
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger("receiver")

OFFSET_KEY = "telegram.offset"
ERROR_BACKOFF_SECONDS = 5


def handle_update(store: Store, tg: TelegramAPI, settings: Settings, update: dict) -> str:
    """Process one Telegram update. Returns a short outcome label (for logs/tests)."""
    msg = update.get("message")
    if not msg:
        return "ignored:not-a-message"
    sender = msg.get("from") or {}
    user_id = sender.get("id")
    chat_id = (msg.get("chat") or {}).get("id")
    text = msg.get("text") or msg.get("caption") or ""

    with store.tx() as tx:
        tenant = tx.tenant_by_telegram(user_id) if user_id else None
        if tenant is None:
            if settings.owner_telegram_user_id and user_id == settings.owner_telegram_user_id:
                tenant = tx.create_tenant(
                    telegram_user_id=user_id, telegram_chat_id=chat_id,
                    display_name=sender.get("first_name") or sender.get("username"),
                    actor="receiver")
                log.info("created owner tenant %s", tenant["id"])
            else:
                log.info("drop: unknown sender id=%s username=%s", user_id, sender.get("username"))
                return "dropped:unknown-sender"
        if tenant["status"] != "active":
            return "dropped:inactive-tenant"
        if not text.strip():
            # Photos/voice/files without text: stored as attachments in a later milestone.
            return "skipped:no-text"

        captured_at = dt.datetime.fromtimestamp(msg.get("date") or time.time(), tz=dt.timezone.utc)
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


def poll_once(store: Store, tg: TelegramAPI, settings: Settings, *, timeout: int = 30) -> int:
    """One long-poll round. The offset is saved only after updates are stored."""
    with store.tx() as tx:
        offset = tx.get_state(OFFSET_KEY)
    updates = tg.get_updates(offset, timeout=timeout)
    for upd in updates:
        try:
            handle_update(store, tg, settings, upd)
        except Exception:
            # Leave the offset before this update so it is retried next round.
            log.exception("failed to store update %s", upd.get("update_id"))
            raise
        with store.tx() as tx:
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
