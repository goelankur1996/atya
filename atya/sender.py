"""Worker 3: outbox -> Telegram. No AI."""

from __future__ import annotations

import logging
import time

from .config import Settings
from .store import Store
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger("sender")


def send_once(store: Store, tg: TelegramAPI) -> int:
    with store.tx(system=True) as tx:
        rows = tx.claim_outbox(limit=20)
    for row in rows:
        try:
            buttons = [[tuple(b) for b in r] for r in row["buttons"]] if row.get("buttons") else None
            tg.send_message(row["chat_id"], row["body"], reply_to=row["reply_to_message_id"],
                            buttons=buttons)
            ok, err = True, None
        except TelegramError as e:
            ok, err = False, str(e)
            log.warning("send failed for %s: %s", row["id"], e)
        with store.tx(system=True) as tx:
            tx.finish_outbox(row["id"], ok=ok, error=err)
    return len(rows)


def run(store: Store, tg: TelegramAPI, settings: Settings) -> None:
    log.info("sender started")
    while True:
        try:
            if send_once(store, tg) == 0:
                time.sleep(1)
        except Exception:
            log.exception("sender loop error")
            time.sleep(settings.poll_interval)
