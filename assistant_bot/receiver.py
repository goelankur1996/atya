"""Worker 1 — Telegram long-poll → Notion Inbox row."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import Config
from .notion import (
    Notion, NotionError, rich_text_value, title_value, paragraph_blocks,
)

log = logging.getLogger("receiver")

LONG_POLL_TIMEOUT = 50
POLL_BACKOFF_SECONDS = 5
NOTION_RETRY_SLEEP = 10


def _offset_path(cfg: Config) -> Path:
    return cfg.state_dir / "telegram_polling.json"


def _load_offset(cfg: Config) -> int | None:
    p = _offset_path(cfg)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("offset")
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read offset file (%s); starting fresh", e)
        return None


def _save_offset(cfg: Config, offset: int) -> None:
    p = _offset_path(cfg)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    tmp.replace(p)


def _get_updates(cfg: Config, offset: int | None) -> list[dict]:
    params: dict = {"timeout": LONG_POLL_TIMEOUT, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{cfg.telegram_bot_token}/getUpdates",
            params=params, timeout=LONG_POLL_TIMEOUT + 10,
        )
        r.raise_for_status()
        body = r.json()
    except requests.RequestException as e:
        log.warning("getUpdates failed: %s", e)
        time.sleep(POLL_BACKOFF_SECONDS)
        return []
    if not body.get("ok"):
        log.error("Telegram API not ok: %s", body)
        time.sleep(POLL_BACKOFF_SECONDS)
        return []
    return body.get("result", [])


def _inbox_exists(notion: Notion, ds_id: str, source_message_id: str) -> bool:
    rows = notion.query(
        ds_id,
        filter={"property": "Source Message ID",
                "rich_text": {"equals": source_message_id}},
        page_size=1, max_pages=1,
    )
    return len(rows) > 0


def _write_inbox(notion: Notion, ds_id: str, *, text: str, sender: str,
                 source_message_id: str, captured_at: str) -> str:
    first_line = text.splitlines()[0] if text else ""
    title = first_line[:60] or "(empty)"
    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)] if text else []
    properties = {
        "Title": {"title": title_value(title)},
        "Source": {"select": {"name": "telegram"}},
        "Sender": {"rich_text": rich_text_value(sender[:2000])},
        "Source Message ID": {"rich_text": rich_text_value(source_message_id)},
        "Captured At": {"date": {"start": captured_at}},
        "Status": {"select": {"name": "pending"}},
    }
    if chunks:
        properties["Raw Content"] = {"rich_text": rich_text_value(chunks[0])}
    resp = notion.create_page(
        parent_ds_id=ds_id, properties=properties,
        children=paragraph_blocks(text) if text else None,
    )
    return resp.get("id", "")


def _process_message(cfg: Config, notion: Notion, msg: dict) -> None:
    sender = msg.get("from") or {}
    sender_id = sender.get("id")
    if sender_id not in cfg.telegram_allowed_user_ids:
        log.info("drop: non-allowlisted sender id=%s username=%s",
                 sender_id, sender.get("username"))
        return

    text = msg.get("text") or msg.get("caption") or ""
    if not text:
        log.info("skip: non-text message id=%s from %s",
                 msg.get("message_id"), sender_id)
        return

    message_id = msg.get("message_id")
    chat_id = (msg.get("chat") or {}).get("id")
    date_unix = msg.get("date") or int(time.time())
    captured_at = datetime.fromtimestamp(date_unix, tz=timezone.utc).isoformat()
    source_message_id = f"telegram:{chat_id}:{message_id}"
    sender_label = sender.get("username") or str(sender_id)

    if _inbox_exists(notion, cfg.inbox_ds_id, source_message_id):
        log.info("dedup: %s already in Inbox", source_message_id)
        return

    page_id = _write_inbox(
        notion, cfg.inbox_ds_id,
        text=text, sender=sender_label,
        source_message_id=source_message_id, captured_at=captured_at,
    )
    log.info("Inbox <- %s (%d chars) from %s [page %s]",
             source_message_id, len(text), sender_label, page_id)


def run(cfg: Config, notion: Notion) -> None:
    if not cfg.telegram_allowed_user_ids:
        log.warning(
            "TELEGRAM_ALLOWED_USER_IDS is empty — every message will be dropped. "
            "Send yourself a message, watch logs for your sender id, add it to .env, restart."
        )
    log.info("starting. allowlist=%s inbox=%s",
             sorted(cfg.telegram_allowed_user_ids), cfg.inbox_ds_id)

    offset = _load_offset(cfg)
    log.info("resuming from offset=%s", offset)

    while True:
        try:
            updates = _get_updates(cfg, offset)
            for update in updates:
                update_id = update["update_id"]
                msg = update.get("message")
                try:
                    if msg is not None:
                        _process_message(cfg, notion, msg)
                except NotionError as e:
                    log.error("Notion write failed for update_id=%s: %s — will retry",
                              update_id, e)
                    time.sleep(NOTION_RETRY_SLEEP)
                    break  # do not advance offset
                except Exception:
                    log.exception("unexpected error on update_id=%s — skipping", update_id)
                offset = update_id + 1
                _save_offset(cfg, offset)
        except Exception:
            log.exception("top-level loop error — continuing")
            time.sleep(POLL_BACKOFF_SECONDS)
