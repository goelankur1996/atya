"""Spending caps.

Per user, per day (in their timezone): messages processed, voice-note minutes,
PDF pages read and model tokens. Plus one cap on tokens for everyone together
per UTC day, so a burst can't run through the credits. The owner is exempt.

A message over a cap is kept (inbox, and its file or audio) but not processed;
the user is told once that day and can resend, or tap Retry on the web,
the next day. When the all-users cap is hit, the owner is told once that day.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .config import Settings
from .store import Store

log = logging.getLogger("limits")
ALL = "all-users"           # the reason given when the everyone-together cap is reached


def is_owner(tenant: dict, settings: Settings) -> bool:
    return bool(settings.owner_telegram_user_id) and tenant.get("telegram_user_id") == settings.owner_telegram_user_id


def tokens_used(llm: Any) -> tuple[int, int]:
    usage = getattr(llm, "usage", None) or {}
    return (int(sum(u.get("in", 0) for u in usage.values())), int(sum(u.get("out", 0) for u in usage.values())))


def over(usage: dict, settings: Settings, *, voice: bool = False, pdf: bool = False) -> str | None:
    """The cap this user has reached today, as words for them; None if within limits."""
    if settings.daily_messages and usage["messages"] >= settings.daily_messages:
        return f"{settings.daily_messages} messages a day"
    if settings.daily_tokens and usage["tokens_in"] + usage["tokens_out"] >= settings.daily_tokens:
        return "today's processing allowance"
    if voice and settings.daily_voice_minutes and usage["voice_seconds"] >= settings.daily_voice_minutes * 60:
        m = settings.daily_voice_minutes
        return f"{m} minute{'s' if m != 1 else ''} of voice notes a day"
    if pdf and settings.daily_pdf_pages and usage["pdf_pages"] >= settings.daily_pdf_pages:
        return f"{settings.daily_pdf_pages} PDF pages a day"
    return None


def all_over(store: Store, settings: Settings) -> bool:
    if not settings.daily_tokens_all:
        return False
    with store.tx(system=True) as tx:
        return tx.add_tokens_all(dt.datetime.now(dt.timezone.utc).date(), 0) >= settings.daily_tokens_all


def record(store: Store, tenant_id, day: dt.date, *, messages: int = 0, voice_seconds: int = 0,
           pdf_pages: int = 0, tokens: tuple[int, int] = (0, 0), held: int = 0) -> None:
    with store.tx(tenant_id) as tx:
        tx.add_usage(tenant_id, day, messages=messages, voice_seconds=voice_seconds, pdf_pages=pdf_pages,
                     tokens_in=tokens[0], tokens_out=tokens[1], held=held)
    if tokens[0] or tokens[1]:
        with store.tx(system=True) as tx:
            tx.add_tokens_all(dt.datetime.now(dt.timezone.utc).date(), tokens[0] + tokens[1])


def tell_owner_once(store: Store, settings: Settings, text: str) -> None:
    key = f"limits:owner-told:{dt.datetime.now(dt.timezone.utc).date().isoformat()}"
    with store.tx(system=True) as tx:
        if tx.get_state(key):
            return
        tx.set_state(key, True)
        owner = tx.tenant_by_telegram(settings.owner_telegram_user_id) if settings.owner_telegram_user_id else None
        if owner:
            tx.add_outbox(owner["id"], chat_id=owner["telegram_chat_id"], body=text, priority="alert",
                          actor="limits")
    log.warning("all-users cap reached: %s", text)
