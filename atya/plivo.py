"""Real phone calls through Plivo (iRefill's account, ATYA's number +91 80 3177 2892).

How one attempt goes:
1. The calls worker asks Plivo to dial the user's number (their WhatsApp number), with three
   callback URLs that carry a fresh random secret for this attempt.
2. When the user picks up, Plivo fetches /plivo/answer/<secret> (the web app), which answers
   with a <Stream> pointing at wss://<domain>/plivo/ws/<secret>.
3. Plivo opens that WebSocket to the audio bridge (callbridge.py), which talks to Nova 2 Sonic
   and, when the call ends, saves the transcript and writes the call's `outcome`.
4. Plivo posts /plivo/hangup/<secret> when the call ends (answered or not) with the cause.
The worker meanwhile watches the calls row and turns what it sees into the attempt's result.

Plivo's rules for India: only Indian numbers are dialled (both ends of a call must be in India).
"""
from __future__ import annotations

import asyncio
import logging
import secrets as pysecrets
from xml.sax.saxutils import escape

import requests

from .config import Settings
from .store import Store

log = logging.getLogger(__name__)

API = "https://api.plivo.com/v1/Account/{auth_id}/Call/"
RING_SECONDS = 45                 # how long the phone rings before it counts as not answered
SETTLE_SECONDS = 60               # after hangup, how long to wait for the bridge to finish saving
STREAM_RATE = 16000               # Plivo <-> bridge audio: 16 kHz, 16-bit, mono (Sonic's input rate)


def dialable(number: str | None) -> bool:
    """An Indian mobile or landline in digits with the country code (e.g. 919711966131)."""
    return bool(number and number.isdigit() and number.startswith("91") and len(number) == 12)


def line_for(settings: Settings, tenant: dict) -> str | None:
    """How to call this user: 'phone' (a real call), 'telegram', or None (no way to reach them)."""
    if settings.plivo_on and dialable(tenant.get("whatsapp_phone")):
        return "phone"
    if settings.tg_caller_on and tenant.get("telegram_user_id"):
        return "telegram"
    return None


def display_number(settings: Settings) -> str:
    """+91 80 3177 2892 style, for telling users which number will call."""
    n = settings.plivo_number or ""
    if len(n) == 12 and n.startswith("91"):
        return f"+91 {n[2:4]} {n[4:8]} {n[8:]}"
    return f"+{n}" if n else ""


def urls(settings: Settings, secret: str) -> dict:
    base = (settings.web_base_url or "").rstrip("/")
    ws = "wss://" + base.split("://", 1)[-1]
    return {"answer": f"{base}/plivo/answer/{secret}", "hangup": f"{base}/plivo/hangup/{secret}",
            "stream": f"{ws}/plivo/ws/{secret}"}


def answer_xml(settings: Settings, secret: str | None) -> str:
    """What Plivo does once the user picks up: stream both directions to the bridge until it hangs up."""
    if not secret:
        return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'
    stream = escape(urls(settings, secret)["stream"])
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-l16;rate={STREAM_RATE}">'
            f'{stream}</Stream></Response>')


def hangup_summary(form: dict) -> str:
    """Plivo's hangup callback in one line, e.g. 'busy|USER_BUSY|Busy Line|17'."""
    keys = ("CallStatus", "HangupCause", "HangupCauseName", "HangupCauseCode", "HangupSource")
    return "|".join(str(form.get(k, "")) for k in keys)


def result_from_hangup(summary: str) -> str:
    """An unanswered call's hangup -> 'declined', 'missed' or 'failed'."""
    s = summary.lower()
    if "violates_media_anchoring" in s or "media anchoring" in s:
        log.error("Plivo hung up for media anchoring: the audio bridge must run in India (%s)", summary)
        return "failed"
    if any(w in s for w in ("busy", "reject", "declin")):
        return "declined"
    if any(w in s for w in ("no-answer", "no_answer", "no answer", "timeout", "cancel", "unallocated")):
        return "missed" if "unallocated" not in s else "failed"
    return "failed"


def dial(settings: Settings, to: str, secret: str, time_limit: int, http=requests) -> str | None:
    """Ask Plivo to place the call. Returns Plivo's request id, or None if it refused."""
    u = urls(settings, secret)
    try:
        r = http.post(API.format(auth_id=settings.plivo_auth_id), auth=(settings.plivo_auth_id, settings.plivo_auth_token),
                      json={"from": settings.plivo_number, "to": to, "answer_url": u["answer"],
                            "answer_method": "POST", "hangup_url": u["hangup"], "hangup_method": "POST",
                            "ring_timeout": RING_SECONDS, "time_limit": time_limit}, timeout=20)
    except requests.RequestException as e:
        log.error("Plivo call request failed: %s", type(e).__name__)
        return None
    if r.status_code not in (200, 201):
        log.error("Plivo refused the call (%s): %s", r.status_code, r.text[:300])
        return None
    body = r.json() if r.content else {}
    uuid = body.get("request_uuid")
    return (uuid[0] if isinstance(uuid, list) and uuid else uuid) or "?"


def hang_up(settings: Settings, call_uuid: str, http=requests) -> None:
    try:
        http.delete(API.format(auth_id=settings.plivo_auth_id) + f"{call_uuid}/",
                    auth=(settings.plivo_auth_id, settings.plivo_auth_token), timeout=10)
    except requests.RequestException:
        log.warning("couldn't hang up the Plivo call", exc_info=True)


async def place_call(store: Store, settings: Settings, tenant: dict, call: dict, *, budget: int,
                     http=requests, poll: float = 1.0) -> str:
    """One attempt by phone. Returns 'answered', 'missed', 'declined' or 'failed'."""
    secret = pysecrets.token_urlsafe(24)
    with store.tx(system=True) as tx:
        tx.start_phone_attempt(call["id"], secret)
    request_id = await asyncio.to_thread(dial, settings, tenant["whatsapp_phone"], secret, budget + 30, http)
    if request_id is None:
        return "failed"
    log.info("phone call %s placed (%s)", str(call["id"])[:8], call["kind"])
    loop = asyncio.get_running_loop()
    ring_until = loop.time() + RING_SECONDS + 30
    talk_until = None
    settle_until = None
    while True:
        await asyncio.sleep(poll)
        with store.tx(system=True) as tx:
            row = tx.call(call["id"])
        if row is None:
            return "failed"
        if row["outcome"]:
            return row["outcome"]
        now = loop.time()
        if row["answered_at"] is not None:
            talk_until = talk_until or now + budget + 120
            if row["hangup_cause"]:
                settle_until = settle_until or now + SETTLE_SECONDS
                if now > settle_until:
                    log.warning("call ended but the audio bridge never reported back")
                    return "answered"
            elif now > talk_until:
                log.warning("call ran past its time with no report from the audio bridge")
                return "answered"
        elif row["hangup_cause"]:
            log.info("phone call not answered: %s", row["hangup_cause"])
            return result_from_hangup(row["hangup_cause"])
        elif now > ring_until:
            log.warning("no answer and no hangup report from Plivo: is %s reachable from Plivo?",
                        settings.web_base_url)
            return "missed"
