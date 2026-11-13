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
    # WhatsApp Business Account id (for creating message templates).
    whatsapp_waba_id: str | None = None
    # The business number in digits (918920396095), for wa.me links in invites.
    whatsapp_number: str | None = None
    # Which models: "anthropic" (Claude API) or "bedrock" (models on Amazon Bedrock).
    llm_provider: str = "anthropic"
    bedrock_fast_model: str | None = None
    bedrock_main_model: str | None = None
    aws_region: str = "us-east-1"
    # Voice notes (Amazon Transcribe): on when VOICE_NOTES=on; audio is staged in this bucket.
    voice_notes: bool = False
    transcribe_bucket: str | None = None
    # Spending caps, per user per day (owner exempt); 0 means no cap.
    daily_messages: int = 150
    daily_voice_minutes: int = 30
    daily_pdf_pages: int = 300
    daily_tokens: int = 1_500_000
    daily_research: int = 10
    # Web research (Nova web grounding): on when RESEARCH=on.
    research: bool = False
    research_model: str = "us.amazon.nova-2-lite-v1:0"
    # All users together, per UTC day.
    daily_tokens_all: int = 20_000_000
    # The owner's own schema file (schema/<name>.yaml); everyone else gets "default".
    owner_schema: str | None = "ankur"
    # Google (calendar + contacts' birthdays, read-only). All three set, or it stays off.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    connections_key: str | None = None      # Fernet key that encrypts stored tokens

    @property
    def google_on(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.connections_key
                    and self.web_base_url)

    # Contact address shown on /privacy.
    privacy_email: str | None = None

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

    def num(name: str, default: int) -> int:
        v = os.environ.get(name, "").strip().replace("_", "").replace(",", "")
        return int(v) if v else default

    provider = (os.environ.get("LLM_PROVIDER", "").strip() or "anthropic").lower()
    if provider not in ("anthropic", "bedrock"):
        raise RuntimeError(f"LLM_PROVIDER must be anthropic or bedrock, not {provider!r}")

    owner = os.environ.get("OWNER_TELEGRAM_USER_ID", "").strip()
    return Settings(
        database_url=need("DATABASE_URL"),
        telegram_bot_token=need("TELEGRAM_BOT_TOKEN"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip() if provider == "bedrock"
        else need("ANTHROPIC_API_KEY"),
        llm_provider=provider,
        bedrock_fast_model=opt("BEDROCK_FAST_MODEL"),
        bedrock_main_model=opt("BEDROCK_MAIN_MODEL"),
        aws_region=os.environ.get("AWS_REGION", "").strip() or "us-east-1",
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
        whatsapp_waba_id=opt("WHATSAPP_WABA_ID"),
        privacy_email=opt("PRIVACY_EMAIL"),
        google_client_id=opt("GOOGLE_CLIENT_ID"),
        google_client_secret=opt("GOOGLE_CLIENT_SECRET"),
        connections_key=opt("CONNECTIONS_KEY"),
        owner_schema=os.environ.get("OWNER_SCHEMA", "ankur").strip() or None,
        daily_messages=num("LIMIT_DAILY_MESSAGES", 150),
        daily_voice_minutes=num("LIMIT_DAILY_VOICE_MINUTES", 30),
        daily_pdf_pages=num("LIMIT_DAILY_PDF_PAGES", 300),
        daily_tokens=num("LIMIT_DAILY_TOKENS", 1_500_000),
        daily_tokens_all=num("LIMIT_DAILY_TOKENS_ALL", 20_000_000),
        daily_research=num("LIMIT_DAILY_RESEARCH", 10),
        research=(os.environ.get("RESEARCH", "").strip().lower() in ("on", "1", "true", "yes")),
        research_model=os.environ.get("RESEARCH_MODEL", "").strip() or "us.amazon.nova-2-lite-v1:0",
        voice_notes=(os.environ.get("VOICE_NOTES", "").strip().lower() in ("on", "1", "true", "yes")),
        transcribe_bucket=opt("TRANSCRIBE_BUCKET") or opt("BACKUP_BUCKET"),
        whatsapp_number="".join(c for c in os.environ.get("WHATSAPP_NUMBER", "") if c.isdigit()) or None,
    )
