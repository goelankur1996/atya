"""The audio bridge for real phone calls: Plivo's audio stream <-> Nova 2 Sonic.

Runs as its own service (`python -m atya.callbridge`, port 8099; Caddy sends /plivo/ws/* here),
so it can move to a server in India if Plivo requires the audio to end there.

Plivo opens wss://<domain>/plivo/ws/<secret> once the user picks up (see plivo.py), sends
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


class Bridge:
    def __init__(self, store: Store, settings: Settings, llm, embedder, sonic_factory=None):
        self.store = store
        self.settings = settings
        self.llm = llm
        self.embedder = embedder
        self.sonic_factory = sonic_factory          # tests swap in a fake voice model

    def claim(self, secret: str) -> tuple[dict, dict] | None:
        """The call this stream is for, marked answered; None if unknown or already streaming."""
        with self.store.tx(system=True) as tx:
            call = tx.call_by_secret(secret)
            if call is None or not tx.mark_call_answered(call["id"]):
                return None
            tenant = tx.tenant(call["tenant_id"])
        return (call, tenant) if tenant else None

    def report(self, call: dict, outcome: str) -> None:
        with self.store.tx(system=True) as tx:
            tx.set_call_outcome(call["id"], outcome)

    async def handle(self, ws, path: str) -> None:
        secret = path.rstrip("/").rsplit("/", 1)[-1]
        claimed = self.claim(secret)
        if claimed is None:
            log.warning("audio stream for an unknown or finished call; closing")
            await ws.close()
            return
        call, tenant = claimed
        try:
            await self.talk(ws, call, tenant)
        except Exception:
            log.exception("phone call %s failed", str(call["id"])[:8])
            self.report(call, "failed")

    async def talk(self, ws, call: dict, tenant: dict) -> None:
        started = dt.datetime.now(dt.timezone.utc)
        answered = time.monotonic()
        budget = wakecall.seconds_left(self.store, self.settings, tenant)
        prompt, tools = await wakecall.prepare(self.store, self.settings, self.llm, self.embedder, tenant, call)
        outq: asyncio.Queue[bytes] = asyncio.Queue()
        down = Down24to16()
        state = {"stream_id": None, "call_uuid": None, "clear": False}
        started_ev = asyncio.Event()

        def on_audio(pcm24: bytes) -> None:
            pcm = down.feed(pcm24)
            for i in range(0, len(pcm), CHUNK_OUT):
                outq.put_nowait(pcm[i:i + CHUNK_OUT])

        def on_interrupt() -> None:                  # the user talked over ATYA: drop what's queued
            down.clear()
            while not outq.empty():
                outq.get_nowait()
            state["clear"] = True
            outq.put_nowait(b"")                     # wake the sender

        make = self.sonic_factory or _sonic
        sonic = make(prompt=prompt, voice=self.settings.tg_call_voice, region=self.settings.aws_region,
                     tools=tools, on_audio=on_audio, on_interrupt=on_interrupt)

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
                    started_ev.set()
                elif kind == "media":
                    started_ev.set()
                    media = ev.get("media") or {}
                    if media.get("track", "inbound") != "inbound":
                        continue
                    pending.extend(base64.b64decode(media.get("payload", "")))
                    if len(pending) >= CHUNK_IN:
                        await sonic.send_audio(bytes(pending))
                        pending.clear()
                elif kind == "stop":
                    return

        async def send():
            while True:
                chunk = await outq.get()
                if state["clear"]:
                    state["clear"] = False
                    if state["stream_id"]:
                        await ws.send(json.dumps({"event": "clearAudio", "streamId": state["stream_id"]}))
                if chunk:
                    await ws.send(play_event(chunk))

        try:
            await sonic.open()
        except Exception:
            log.exception("voice model unavailable; hanging up")
            self.report(call, "failed")
            await self.hang_up(ws, state)
            return
        receiver = asyncio.create_task(receive())
        sender = asyncio.create_task(send())
        try:
            await asyncio.wait_for(started_ev.wait(), timeout=START_WAIT)
        except asyncio.TimeoutError:
            log.warning("no start event from Plivo; talking anyway")
        await sonic.say_first(wakecall.GREETING)
        done = asyncio.create_task(sonic.done.wait())
        await asyncio.wait([receiver, done], timeout=budget, return_when=asyncio.FIRST_COMPLETED)
        hung_up_by_user = receiver.done()
        for t in (receiver, sender, done):
            t.cancel()
        await sonic.close()
        if not hung_up_by_user:
            await self.hang_up(ws, state)
        wakecall.finish_talk(self.store, tenant, call, sonic, time.monotonic() - answered, started)
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
        await asyncio.Future()


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
    llm, embedder = wakecall.make_models(settings)
    asyncio.run(serve(Bridge(store, settings, llm, embedder)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
