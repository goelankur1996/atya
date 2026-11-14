"""Voice calls to the owner from a second Telegram account (owner only).

This is unofficial user-account automation (Telethon + py-tgcalls/NTgCalls), so it is
used only to call the owner, never other users.

Step 1 is a ring test that settles whether live audio flows both ways:

    docker compose exec -T app python -m atya.tgcall test

It rings the owner, beeps, listens for 5 seconds, beeps twice, plays back what it
heard, and hangs up. It prints how much audio it received and how loud it was.
"""
from __future__ import annotations

import array
import asyncio
import logging
import math
import sys
import time

from .config import Settings, load_settings

log = logging.getLogger(__name__)

RATE = 48_000                       # Telegram calls carry 48 kHz audio
FRAME_MS = 10                       # NTgCalls takes external audio in 10 ms frames
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2    # mono, signed 16-bit


def tone(ms: int, hz: float = 880.0, volume: float = 0.3) -> bytes:
    n = RATE * ms // 1000
    fade = min(n // 2, RATE // 200)          # 5 ms fade in and out, so it doesn't click
    out = array.array("h")
    for i in range(n):
        env = min(1.0, i / fade if fade else 1.0, (n - i) / fade if fade else 1.0)
        out.append(int(32767 * volume * env * math.sin(2 * math.pi * hz * i / RATE)))
    return out.tobytes()


def silence(ms: int) -> bytes:
    return bytes(RATE * ms // 1000 * 2)


def frames(pcm: bytes) -> list[bytes]:
    """Split PCM into 10 ms frames, padding the last one with silence."""
    out = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
    if out and len(out[-1]) < FRAME_BYTES:
        out[-1] = out[-1] + bytes(FRAME_BYTES - len(out[-1]))
    return out


def levels(pcm: bytes) -> tuple[float, float]:
    """(peak, rms) as fractions of full scale."""
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return 0.0, 0.0
    peak = max(abs(x) for x in a) / 32768
    rms = math.sqrt(sum(x * x for x in a) / len(a)) / 32768
    return peak, rms


class Caller:
    """One logged-in second account that can ring the owner and stream audio both ways."""

    def __init__(self, settings: Settings):
        if not settings.tg_caller_on:
            raise RuntimeError("Calling isn't set up: TG_CALLER_API_ID, TG_CALLER_API_HASH and "
                               "TG_CALLER_SESSION must be in SSM (see deploy/tg-caller-login.py)")
        from pytgcalls import PyTgCalls
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        self.settings = settings
        self.client = TelegramClient(StringSession(settings.tg_caller_session), int(settings.tg_caller_api_id),
                                     settings.tg_caller_api_hash, device_model="ATYA server",
                                     system_version="Linux", app_version="1.0")
        self.calls = PyTgCalls(self.client)
        self.heard = bytearray()
        self.listening = False
        self.ended = asyncio.Event()

    async def start(self) -> None:
        from pytgcalls import filters
        from pytgcalls.types import ChatUpdate, Device, Direction, StreamFrames

        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("The caller account's session has expired: run deploy/tg-caller-login.py again")
        await self.client.get_dialogs(limit=100)        # so the owner's account can be looked up

        @self.calls.on_update(filters.stream_frame(Direction.INCOMING, Device.MICROPHONE))
        async def _heard(_, update: StreamFrames):
            if self.listening:
                for f in update.frames:
                    self.heard.extend(f.frame)

        @self.calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def _ended(_, update):
            log.info("call ended (%s)", update.status)
            self.ended.set()

        await self.calls.start()

    async def ring(self, user_id: int, timeout: int = 45) -> None:
        """Returns once the call is answered; raises if declined or not answered."""
        from pytgcalls.types import CallConfig, ExternalMedia, MediaStream, RecordStream
        from pytgcalls.types.raw import AudioParameters

        self.ended.clear()
        await self.calls.play(user_id, MediaStream(ExternalMedia.AUDIO, AudioParameters(RATE, 1),
                                                   video_flags=MediaStream.Flags.IGNORE),
                              CallConfig(timeout=timeout))
        await self.calls.record(user_id, RecordStream(True, AudioParameters(RATE, 1)))

    async def say(self, user_id: int, pcm: bytes) -> None:
        """Stream PCM into the call in real time (10 ms frames, paced against the clock)."""
        from pytgcalls.types import Device

        start = time.monotonic()
        for i, frame in enumerate(frames(pcm)):
            if self.ended.is_set():
                return
            await self.calls.send_frame(user_id, Device.MICROPHONE, frame)
            ahead = start + (i + 1) * FRAME_MS / 1000 - time.monotonic()
            if ahead > 0:
                await asyncio.sleep(ahead)

    async def listen(self, seconds: float) -> bytes:
        self.heard.clear()
        self.listening = True
        try:
            await asyncio.wait_for(self.ended.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self.listening = False
        return bytes(self.heard)

    async def hang_up(self, user_id: int) -> None:
        try:
            await self.calls.leave_call(user_id)
        except Exception:                                  # already ended on their side
            log.debug("leave_call failed", exc_info=True)

    async def stop(self) -> None:
        await self.client.disconnect()


async def ring_test(settings: Settings) -> int:
    user_id = settings.owner_telegram_user_id
    caller = Caller(settings)
    await caller.start()
    try:
        print(f"Ringing {user_id}... answer on your main Telegram account.")
        try:
            await caller.ring(user_id)
        except Exception as e:
            print(f"Call not answered or failed: {type(e).__name__}: {e}")
            return 1
        print("Answered. Beep, then say anything for 5 seconds.")
        await caller.say(user_id, silence(800) + tone(300))
        heard = await caller.listen(5)
        await caller.say(user_id, tone(120, 660) + silence(80) + tone(120, 660) + silence(300))
        secs = len(heard) / 2 / RATE
        peak, rms = levels(heard)
        print(f"Heard {secs:.1f} s of audio from you (peak {peak:.0%}, average {rms:.1%}). Playing it back.")
        if heard:
            await caller.say(user_id, heard)
        await caller.say(user_id, silence(300) + tone(400, 520))
        await caller.hang_up(user_id)
        if secs < 3:
            print("RESULT: audio TO you works; audio FROM you did not come through (or too little).")
            return 2
        print("RESULT: if you heard your own voice played back, live audio works both ways.")
        return 0
    finally:
        await caller.stop()


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    if argv[:1] != ["test"]:
        print(__doc__)
        return 2
    return asyncio.run(ring_test(load_settings()))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
