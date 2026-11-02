"""Worker 3 — Outbox (pending+telegram) → Telegram sendMessage."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

from .config import Config
from .notion import (
    Notion, NotionError, get_property_text, rich_text_value,
)

log = logging.getLogger("sender")
RICH_TEXT_LIMIT = 2000


class TelegramSendError(Exception):
    pass


def _send_telegram(bot_token: str, chat_id: str, text: str) -> dict:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=30)
    except requests.RequestException as e:
        raise TelegramSendError(f"network error: {e}") from e
    try:
        body = r.json()
    except ValueError as e:
        raise TelegramSendError(f"non-JSON response (HTTP {r.status_code}): {r.text[:200]}") from e
    if not r.ok or not body.get("ok"):
        raise TelegramSendError(
            f"HTTP {body.get('error_code') or r.status_code}: "
            f"{body.get('description') or r.text[:200]}"
        )
    return body.get("result", {})


def _query_pending(notion: Notion, outbox_ds_id: str) -> list[dict]:
    now_iso = datetime.now(timezone.utc).isoformat()
    return notion.query(
        outbox_ds_id,
        filter={"and": [
            {"property": "Status", "select": {"equals": "pending"}},
            {"property": "Channel", "select": {"equals": "telegram"}},
            {"or": [
                {"property": "Scheduled For", "date": {"is_empty": True}},
                {"property": "Scheduled For", "date": {"on_or_before": now_iso}},
            ]},
        ]},
        sorts=[{"timestamp": "created_time", "direction": "ascending"}],
        page_size=50, max_pages=1,
    )


def _update_status(notion: Notion, page_id: str, status: str, *,
                   sent_at: str | None = None, send_error: str | None = None) -> None:
    properties: dict = {"Status": {"select": {"name": status}}}
    if sent_at:
        properties["Sent At"] = {"date": {"start": sent_at}}
    if send_error is not None:
        properties["Send Error"] = {"rich_text": rich_text_value(send_error[:RICH_TEXT_LIMIT])}
    notion.update_page(page_id, properties)


def _process_row(cfg: Config, notion: Notion, row: dict) -> None:
    page_id = row["id"]
    chat_id = get_property_text(row, "Chat ID").strip()
    body = get_property_text(row, "Body")

    if not chat_id:
        log.warning("row %s missing Chat ID -> failed", page_id[:8])
        try:
            _update_status(notion, page_id, "failed", send_error="missing chat id")
        except NotionError as e:
            log.error("could not mark %s failed: %s", page_id[:8], e)
        return
    if not body:
        log.warning("row %s empty Body -> failed", page_id[:8])
        try:
            _update_status(notion, page_id, "failed", send_error="empty body")
        except NotionError as e:
            log.error("could not mark %s failed: %s", page_id[:8], e)
        return

    try:
        _update_status(notion, page_id, "sending")
    except NotionError as e:
        log.warning("could not claim %s: %s — retry next poll", page_id[:8], e)
        return

    try:
        result = _send_telegram(cfg.telegram_bot_token, chat_id, body)
    except TelegramSendError as e:
        log.error("send failed for %s: %s", page_id[:8], e)
        try:
            _update_status(notion, page_id, "failed", send_error=str(e))
        except NotionError as ne:
            log.error("could not mark %s failed: %s", page_id[:8], ne)
        return

    sent_msg_id = result.get("message_id")
    try:
        _update_status(notion, page_id, "sent",
                       sent_at=datetime.now(timezone.utc).isoformat())
    except NotionError as e:
        log.error("sent %s to chat=%s (msg=%s) but could not mark sent: %s",
                  page_id[:8], chat_id, sent_msg_id, e)
        return

    log.info("sent %s to chat=%s (%d chars, telegram msg=%s)",
             page_id[:8], chat_id, len(body), sent_msg_id)


def run(cfg: Config, notion: Notion) -> None:
    log.info("starting. outbox=%s poll=%ds", cfg.outbox_ds_id, cfg.poll_interval)
    while True:
        try:
            rows = _query_pending(notion, cfg.outbox_ds_id)
        except NotionError as e:
            log.warning("query failed: %s — retry", e)
            time.sleep(cfg.poll_interval)
            continue
        except Exception:
            log.exception("unexpected query error — retry")
            time.sleep(cfg.poll_interval)
            continue

        if rows:
            log.info("found %d pending telegram row(s)", len(rows))
        for row in rows:
            try:
                _process_row(cfg, notion, row)
            except Exception:
                log.exception("unexpected error on row %s — continuing",
                              row.get("id", "?")[:8])

        time.sleep(cfg.poll_interval)
