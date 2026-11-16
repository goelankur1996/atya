"""The audio bridge for real phone calls: Plivo's audio stream <-> Nova 2 Sonic.

Runs as its own service (`python -m atya.callbridge`, port 8099; Caddy sends /plivo/ws/* here),
so it can move to a server in India if Plivo requires the audio to end there.

While the phone rings, the bridge opens the voice model and has it say the opening, holding the
audio; it plays OPENING_DELAY after pickup. Plivo opens wss://<domain>/plivo/ws/<secret> once
the user picks up (see plivo.py), sends
`start`, then `media` events (16 kHz 16-bit PCM, base64) and `stop` when the call ends.
We send `playAudio` events back (Sonic speaks at 24 kHz; resampled to 16 kHz) and
`clearAudio` when the user talks over ATYA. When the talk is over we count the minutes,
save the transcript, hang up, and write the call's `outcome` for the calls worker.
"""
from __future__ import annotations

import array
import asyncio
import base64
import datetime as dt
import json
import logging
import time

from . import plivo, wakecall
from .config import Settings
from .store import Store

log = logging.getLogger(__name__)

PORT = 8099
CHUNK_IN = 1024           # bytes of caller audio per Sonic event (32 ms at 16 kHz)
CHUNK_OUT = 6400          # bytes of ATYA's speech per playAudio event (200 ms at 16 kHz)
START_WAIT = 10           # seconds to wait for Plivo's `start` event
OPENING_DELAY = 0.5       # seconds from pickup to ATYA's first word (the opening is generated while it rings)
WATCH_SECONDS = 0.25      # how often to look for newly ringing calls
KEEPALIVE_SECONDS = 0.5   # while ringing: silence to the voice model this often, and check the call is still on
SILENCE = bytes(1600)     # 50 ms of silence at 16 kHz


class Down24to16:
    """24 kHz -> 16 kHz, 16-bit mono, across chunk boundaries: every 3 samples become 2
    (the first kept, the next two averaged)."""

    def __init__(self) -> None:
        self.rest = b""

    def feed(self, pcm24: bytes) -> bytes:
        data = self.rest + pcm24
        usable = len(data) // 6 * 6
        self.rest = data[usable:]
        a = array.array("h")
        a.frombytes(data[:usable])
        out = array.array("h", bytes(len(a) // 3 * 4))
        out[0::2] = a[0::3]
        out[1::2] = array.array("h", ((x + y) // 2 for x, y in zip(a[1::3], a[2::3])))
        return out.tobytes()

    def clear(self) -> None:
        self.rest = b""


def play_event(pcm16: bytes) -> str:
    return json.dumps({"event": "playAudio", "media": {"contentType": "audio/x-l16", "sampleRate": plivo.STREAM_RATE,
                                                       "payload": base64.b64encode(pcm16).decode()}})


class Session:
    """One call's voice session. Opened while the phone rings, so the opening is ready at pickup."""

    def __init__(self, call: dict, tenant: dict):
        self.call, self.tenant = call, tenant
        self.requests: list = []
        self.outq: asyncio.Queue[bytes] = asyncio.Queue()
        self.down = Down24to16()
        self.state = {"stream_id": None, "call_uuid": None, "clear": False}
        self.sonic = None
        self.ready = asyncio.Event()          # Sonic open and the opening asked for (or failed)
        self.failed = False
        self.connected = False
        self.budget = 0

    def on_audio(self, pcm24: bytes) -> None:
        pcm = self.down.feed(pcm24)
        for i in range(0, len(pcm), CHUNK_OUT):
            self.outq.put_nowait(pcm[i:i + CHUNK_OUT])

    def on_interrupt(self) -> None:             # the user talked over ATYA: drop what's queued
        self.down.clear()
        while not self.outq.empty():
            self.outq.get_nowait()
        self.state["clear"] = True
        self.outq.put_nowait(b"")               # wake the sender


class Bridge:
    def __init__(self, store: Store, settings: Settings, sonic_factory=None):
        self.store = store
        self.settings = settings
        self.sonic_factory = sonic_factory          # tests swap in a fake voice model
        self.sessions: dict[str, Session] = {}      # by the attempt's secret

    # --- before pickup: get the opening ready ------------------------------------

    async def watch(self) -> None:
        """Start a session for every phone call that's ringing."""
        while True:
            try:
                with self.store.tx(system=True) as tx:
                    ringing = tx.ringing_phone_calls()
                for call in ringing:
                    if call["secret"] not in self.sessions:
                        self.start(call)
            except Exception:
                log.exception("watching for ringing calls failed")
            await asyncio.sleep(WATCH_SECONDS)

    def start(self, call: dict) -> Session | None:
        with self.store.tx(system=True) as tx:
            tenant = tx.tenant(call["tenant_id"])
        if tenant is None:
            return None
        sess = Session(call, tenant)
        self.sessions[call["secret"]] = sess
        asyncio.create_task(self.warm(sess))
        return sess

    async def warm(self, sess: Session) -> None:
        secret = sess.call["secret"]
        try:
            sess.budget = wakecall.seconds_left(self.store, self.settings, sess.tenant)
            prompt, tools = await wakecall.prepare(self.store, self.settings, sess.tenant, sess.call, sess.requests)
            make = self.sonic_factory or _sonic
            sess.sonic = make(prompt=prompt, voice=self.settings.tg_call_voice, region=self.settings.aws_region,
                              tools=tools, on_audio=sess.on_audio, on_interrupt=sess.on_interrupt)
            await sess.sonic.open()
            await sess.sonic.say_first(wakecall.GREETING)
        except Exception:
            log.exception("voice model unavailable")
            sess.failed = True
        sess.ready.set()
        # Ringing: keep the stream alive with silence until they pick up, or drop it if the call ends.
        while not sess.connected and not sess.failed:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            if sess.connected:
                break
            with self.store.tx(system=True) as tx:
                row = tx.call(sess.call["id"])
            if row is None or row["status"] != "calling" or row["hangup_cause"] or row["secret"] != secret:
                log.info("call %s ended before pickup; dropping its voice session", str(sess.call["id"])[:8])
                if sess.sonic is not None:
                    await sess.sonic.close()
                self.sessions.pop(secret, None)
                return
            if sess.sonic is not None:
                await sess.sonic.send_audio(SILENCE)

    # --- after pickup ---------------------------------------------------------------

    def claim(self, secret: str) -> dict | None:
        """The call this stream is for, marked answered; None if unknown or already streaming."""
        with self.store.tx(system=True) as tx:
            call = tx.call_by_secret(secret)
            if call is None or not tx.mark_call_answered(call["id"]):
                return None
            return tx.call(call["id"])

    def report(self, call: dict, outcome: str) -> None:
        with self.store.tx(system=True) as tx:
            tx.set_call_outcome(call["id"], outcome)

    async def handle(self, ws, path: str) -> None:
        secret = path.rstrip("/").rsplit("/", 1)[-1]
        call = self.claim(secret)
        if call is None:
            log.warning("audio stream for an unknown or finished call; closing")
            await ws.close()
            return
        sess = self.sessions.get(secret) or self.start(call)
        if sess is None:
            await ws.close()
            return
        sess.connected = True
        try:
            await self.talk(ws, sess)
        except Exception:
            log.exception("phone call %s failed", str(call["id"])[:8])
            self.report(call, "failed")
        finally:
            self.sessions.pop(secret, None)

    async def talk(self, ws, sess: Session) -> None:
        call, tenant, state = sess.call, sess.tenant, sess.state
        started = dt.datetime.now(dt.timezone.utc)
        answered = time.monotonic()
        streaming = asyncio.Event()                  # Plivo's `start`: the line is ready for our audio

        async def receive():
            pending = bytearray()
            async for raw in ws:
                try:
                    ev = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                kind = ev.get("event")
                if kind == "start":
                    st = ev.get("start") or {}
                    state["stream_id"] = st.get("streamId") or ev.get("streamId")
                    state["call_uuid"] = st.get("callId")
                    if state["call_uuid"]:
                        with self.store.tx(system=True) as tx:
                            tx.set_call_provider_uuid(call["id"], state["call_uuid"])
                    streaming.set()
                elif kind == "media":
                    streaming.set()
                    media = ev.get("media") or {}
                    if media.get("track", "inbound") != "inbound" or sess.sonic is None:
                        continue
                    pending.extend(base64.b64decode(media.get("payload", "")))
                    if len(pending) >= CHUNK_IN:
                        await sess.sonic.send_audio(bytes(pending))
                        pending.clear()
                elif kind == "stop":
                    return

        async def send():
            try:
                await asyncio.wait_for(streaming.wait(), timeout=START_WAIT)
            except asyncio.TimeoutError:
                log.warning("no start event from Plivo; talking anyway")
            picked = call.get("picked_up_at")
            if picked is not None:                   # the opening starts OPENING_DELAY after pickup, not sooner
                wait = OPENING_DELAY - (dt.datetime.now(dt.timezone.utc) - picked).total_seconds()
                if wait > 0:
                    await asyncio.sleep(wait)
            else:
                await asyncio.sleep(OPENING_DELAY)
            while True:
                chunk = await sess.outq.get()
                if state["clear"]:
                    state["clear"] = False
                    if state["stream_id"]:
                        await ws.send(json.dumps({"event": "clearAudio", "streamId": state["stream_id"]}))
                if chunk:
                    await ws.send(play_event(chunk))

        receiver = asyncio.create_task(receive())
        await sess.ready.wait()
        if sess.failed:
            receiver.cancel()
            self.report(call, "failed")
            await self.hang_up(ws, state)
            return
        sender = asyncio.create_task(send())
        done = asyncio.create_task(sess.sonic.done.wait())
        await asyncio.wait([receiver, done], timeout=sess.budget or None, return_when=asyncio.FIRST_COMPLETED)
        hung_up_by_user = receiver.done()
        for t in (receiver, sender, done):
            t.cancel()
        await sess.sonic.close()
        if not hung_up_by_user:
            await self.hang_up(ws, state)
        wakecall.finish_talk(self.store, tenant, call, sess.sonic, time.monotonic() - answered, started,
                             sess.requests)
        self.report(call, "answered")

    async def hang_up(self, ws, state: dict) -> None:
        if state.get("call_uuid"):
            await asyncio.to_thread(plivo.hang_up, self.settings, state["call_uuid"])
        try:
            await ws.close()
        except Exception:
            pass


def _sonic(*, prompt, voice, region, tools, on_audio, on_interrupt):
    from .sonic import Sonic
    return Sonic(system_prompt=prompt, voice=voice, region=region, tools=tools,
                 on_audio=on_audio, on_interrupt=on_interrupt)


async def serve(bridge: Bridge, port: int = PORT) -> None:
    import websockets

    async def handler(ws):
        path = ws.request.path if getattr(ws, "request", None) else "/"
        if not path.startswith("/plivo/ws/"):
            await ws.close()
            return
        await bridge.handle(ws, path)

    async with websockets.serve(handler, "0.0.0.0", port, max_size=2 ** 20, ping_interval=20):
        log.info("audio bridge listening on :%d", port)
        await bridge.watch()


def main() -> int:
    import sys
    from .config import load_settings
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    settings = load_settings()
    if not settings.plivo_on:
        log.warning("phone calls are off (needs PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_NUMBER, WEB_BASE_URL); idle")
        while True:
            time.sleep(3600)
    store = Store(settings.database_url)
    asyncio.run(serve(Bridge(store, settings)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
