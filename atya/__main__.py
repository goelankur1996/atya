"""Entry point: migrate, then run receiver / processor / sender / web / briefs / reminders as supervised threads.

Workers share nothing but the database. A crash in one is logged and the
worker restarts after 5 s; the others keep running.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from zoneinfo import ZoneInfo

import psycopg

from . import health, research, transcribe, briefs, embeddings, processor, receiver, reminders, sender, web, whatsapp
from .config import load_settings
from .llm import BedrockLLM, ClaudeLLM
from .migrate import migrate
from .schema import load_schema
from .store import Store
from .telegram import Telegram

log = logging.getLogger("main")


def _supervise(name: str, target, *args) -> None:
    while True:
        try:
            target(*args)
            log.warning("[%s] returned unexpectedly; restarting in 5s", name)
        except Exception:
            log.exception("[%s] crashed; restarting in 5s", name)
        time.sleep(5)


def sync_owner_timezone(store: Store, settings) -> None:
    """Keep the owner's account timezone in line with OWNER_TIMEZONE.

    Interim until users set their own timezone during onboarding (milestone 5).
    """
    if not settings.owner_telegram_user_id:
        return
    try:
        ZoneInfo(settings.owner_timezone)
    except Exception:
        log.error("OWNER_TIMEZONE %r is not a valid timezone; ignoring", settings.owner_timezone)
        return
    with store.tx(system=True) as tx:
        t = tx.tenant_by_telegram(settings.owner_telegram_user_id)
        if t and t["timezone"] != settings.owner_timezone:
            tx.set_timezone(t["id"], settings.owner_timezone)


def sync_owner_schema(store: Store, settings) -> None:
    """The owner's account uses their own schema (with personal trackers), if the file exists."""
    if not settings.owner_telegram_user_id or not settings.owner_schema:
        return
    try:
        load_schema(settings.owner_schema)
    except Exception as e:
        log.error("owner schema %r can't be loaded (%s); leaving it as is", settings.owner_schema, e)
        return
    with store.tx(system=True) as tx:
        t = tx.tenant_by_telegram(settings.owner_telegram_user_id)
        if t and t["schema_name"] != settings.owner_schema:
            tx.conn.execute("UPDATE tenants SET schema_name = %s WHERE id = %s", (settings.owner_schema, t["id"]))
            tx.audit(t["id"], actor="system", action="update", target_table="tenants", target_id=t["id"],
                     summary=f"schema set to {settings.owner_schema}")
            log.info("owner account now uses schema %s", settings.owner_schema)
            log.info("owner timezone set to %s", settings.owner_timezone)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    settings = load_settings()
    with psycopg.connect(settings.database_url) as conn:
        applied = migrate(conn)
    log.info("migrations applied: %s", applied or "none")
    load_schema("default")  # fail fast on a broken schema file

    store = Store(settings.database_url)
    sync_owner_timezone(store, settings)
    sync_owner_schema(store, settings)
    tg = Telegram(settings.telegram_bot_token)
    if settings.llm_provider == "bedrock":
        if not (settings.bedrock_fast_model and settings.bedrock_main_model):
            raise RuntimeError("LLM_PROVIDER=bedrock needs BEDROCK_FAST_MODEL and BEDROCK_MAIN_MODEL")
        llm = BedrockLLM(fast_model=settings.bedrock_fast_model, main_model=settings.bedrock_main_model,
                         region=settings.aws_region)
        log.info("models: Bedrock %s (fast), %s (main)", settings.bedrock_fast_model, settings.bedrock_main_model)
    else:
        llm = ClaudeLLM(settings.anthropic_api_key)
    transcriber = None
    if settings.voice_notes and settings.transcribe_bucket:
        transcriber = transcribe.AWSTranscriber(bucket=settings.transcribe_bucket, region=settings.aws_region)
        log.info("voice notes on (Amazon Transcribe)")
    else:
        log.warning("voice notes off (needs VOICE_NOTES=on and a bucket)")
    researcher = None
    if settings.research:
        researcher = research.NovaResearcher(model=settings.research_model, region=settings.aws_region)
        log.info("web research on (%s)", settings.research_model)
    embedder = embeddings.VoyageEmbedder(settings.voyage_api_key) if settings.voyage_api_key else None
    if embedder is None:
        log.warning("VOYAGE_API_KEY not set: embeddings off, search uses full text only")

    workers = [
        ("receiver", receiver.run, (store, tg, settings)),
        ("processor", processor.run, (store, llm, settings, embedder, transcriber, researcher)),
        ("sender", sender.run, (store, tg, settings)),
        ("web", web.run, (store, settings)),
        ("briefs", briefs.run, (store, settings)),
        ("reminders", reminders.run, (store, settings)),
        ("health", health.run, (store, settings)),
    ]
    if settings.whatsapp_on:
        wa = whatsapp.WhatsApp(settings.whatsapp_token, settings.whatsapp_phone_number_id)
        workers += [("whatsapp", whatsapp.run, (store, wa, settings, tg)),
                    ("whatsapp-sender", whatsapp.run_sender, (store, wa, settings))]
    else:
        log.warning("WhatsApp is off (needs WHATSAPP_TOKEN, WHATSAPP_APP_SECRET, "
                    "WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN)")
    if embedder is not None:
        workers.append(("embedder", embeddings.run, (store, embedder, settings)))
    for name, target, args in workers:
        threading.Thread(target=_supervise, args=(name, target, *args), daemon=True, name=name).start()

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    log.info("shutting down")


if __name__ == "__main__":
    main()
