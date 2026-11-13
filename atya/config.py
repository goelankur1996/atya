"""Service-level settings. Per-user settings live in the `tenants` table."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"
SCHEMA_DIR = ROOT / "schema"

# Must match records.embedding in migrations/001_init.sql.
EMBEDDING_DIM = 1024


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


@dataclass(frozen=True)
class Settings:
    database_url: str
    telegram_bot_token: str
    anthropic_api_key: str
    # The owner can create invites; their tenant row is created on their first message.
    owner_telegram_user_id: int | None
    owner_timezone: str = "UTC"
    voyage_api_key: str | None = None
    poll_interval: int = 5
    reclaim_minutes: int = 5
    max_attempts: int = 3
    # Public address of the web view, e.g. https://atya.example.com. Unset: /web is off.
    web_base_url: str | None = None
    web_port: int = 8080
    # WhatsApp Cloud API. All four must be set, or WhatsApp stays off.
    whatsapp_token: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_verify_token: str | None = None
    # The business number in digits (918920396095), for wa.me links in invites.
    whatsapp_number: str | None = None

    @property
    def whatsapp_on(self) -> bool:
        return bool(self.whatsapp_token and self.whatsapp_app_secret and self.whatsapp_phone_number_id
                    and self.whatsapp_verify_token)


def load_settings() -> Settings:
    def need(name: str) -> str:
        v = os.environ.get(name, "").strip()
        if not v:
            print(f"FATAL: {name} is not set", file=sys.stderr)
            sys.exit(1)
        return v

    def opt(name: str) -> str | None:
        return os.environ.get(name, "").strip() or None

    owner = os.environ.get("OWNER_TELEGRAM_USER_ID", "").strip()
    return Settings(
        database_url=need("DATABASE_URL"),
        telegram_bot_token=need("TELEGRAM_BOT_TOKEN"),
        anthropic_api_key=need("ANTHROPIC_API_KEY"),
        owner_telegram_user_id=int(owner) if owner.isdigit() else None,
        owner_timezone=os.environ.get("OWNER_TIMEZONE", "UTC").strip() or "UTC",
        voyage_api_key=os.environ.get("VOYAGE_API_KEY", "").strip() or None,
        poll_interval=int(os.environ.get("POLL_INTERVAL_SECONDS", "5")),
        reclaim_minutes=int(os.environ.get("PROCESSING_RECLAIM_MINUTES", "5")),
        max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")),
        web_base_url=os.environ.get("WEB_BASE_URL", "").strip().rstrip("/") or None,
        web_port=int(os.environ.get("WEB_PORT", "8080")),
        whatsapp_token=opt("WHATSAPP_TOKEN"),
        whatsapp_app_secret=opt("WHATSAPP_APP_SECRET"),
        whatsapp_phone_number_id=opt("WHATSAPP_PHONE_NUMBER_ID"),
        whatsapp_verify_token=opt("WHATSAPP_VERIFY_TOKEN"),
        whatsapp_number="".join(c for c in os.environ.get("WHATSAPP_NUMBER", "") if c.isdigit()) or None,
    )
