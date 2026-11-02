"""Env + logging setup. One copy, used by all three workers."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"FATAL: {name} is not set", file=sys.stderr)
        sys.exit(1)
    return val


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_allowed_user_ids: frozenset[int]
    notion_token: str
    inbox_ds_id: str
    outbox_ds_id: str
    schema_page_id: str
    anthropic_api_key: str
    poll_interval: int
    reclaim_minutes: int
    schema_reload_minutes: int
    state_dir: Path


def load() -> Config:
    raw_allow = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    allow = frozenset(int(x) for x in raw_allow.split(",") if x.strip().isdigit())

    state_dir = Path(os.environ.get("STATE_DIR", "/app/state"))
    state_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_user_ids=allow,
        notion_token=_require("NOTION_TOKEN"),
        inbox_ds_id=_require("INBOX_DATA_SOURCE_ID"),
        outbox_ds_id=_require("OUTBOX_DATA_SOURCE_ID"),
        schema_page_id=_require("SCHEMA_PAGE_ID"),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        poll_interval=_int("POLL_INTERVAL_SECONDS", 10),
        reclaim_minutes=_int("PROCESSING_RECLAIM_MINUTES", 5),
        schema_reload_minutes=_int("SCHEMA_RELOAD_MINUTES", 15),
        state_dir=state_dir,
    )


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )


VALID_TARGET_DBS: frozenset[str] = frozenset({
    "inbox", "outbox", "tasks", "projects", "thoughts",
    "decisions", "people", "reading", "none",
})
VALID_ACTIONS: frozenset[str] = frozenset({"create", "update", "delete", "send", "reject"})
