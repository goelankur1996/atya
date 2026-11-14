"""Voice calls to the owner from a second Telegram account (owner only).

This is unofficial user-account automation (Telethon + py-tgcalls/NTgCalls), so it is
used only to call the owner, never other users.

Step 1 is a ring test that settles whether live audio flows both ways:

    docker compose exec -T app python -m atya.tgcall test

It rings the owner, beeps, listens for 5 seconds, beeps twice, plays back what it
heard, and hangs up. It prints how much audio it received and how loud it was.

Step 2 is a live conversation with Nova 2 Sonic (hang up to end it):

    docker compose exec -T app python -m atya.tgcall check   # the voice model alone, no call
    docker compose exec -T app python -m atya.tgcall talk [voice]     # e.g. talk kiara
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


def down_to_16k(pcm48: bytes) -> bytes:
    """48 kHz -> 16 kHz: average each group of three samples (a cheap low-pass) and keep one."""
    a = array.array("h")
    a.frombytes(pcm48[: len(pcm48) // 6 * 6])
    return array.array("h", ((x + y + z) // 3 for x, y, z in zip(a[0::3], a[1::3], a[2::3]))).tobytes()


def up_from_24k(pcm24: bytes) -> bytes:
    """24 kHz -> 48 kHz: keep each sample and put the midpoint to the next one between them."""
    a = array.array("h")
    a.frombytes(pcm24[: len(pcm24) // 2 * 2])
    if not a:
        return b""
    out = array.array("h", bytes(len(a) * 4))
    out[0::2] = a
    nxt = a[1:] + array.array("h", [a[-1]])
    out[1::2] = array.array("h", ((x + y) // 2 for x, y in zip(a, nxt)))
    return out.tobytes()


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
        self.on_audio = None            # called with each incoming 48 kHz PCM chunk, if set
        self.last_heard = 0.0           # monotonic time of the last incoming audio frame
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
            self.last_heard = time.monotonic()
            for f in update.frames:
                if self.listening:
                    self.heard.extend(f.frame)
                if self.on_audio:
                    self.on_audio(f.frame)

        @self.calls.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def _ended(_, update):
            log.info("call ended (%s)", update.status)
            self.ended.set()

        await self.calls.start()

    async def ring(self, user_id: int, timeout: int = 60) -> None:
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


TALK_PROMPT = (
    "You are ATYA, Ankur's personal assistant, on a phone call with him. This is a test of the call. "
    "Speak naturally and calmly, like a capable assistant, not overly cheerful, in one or two short sentences "
    "per turn. Always speak English. Only if he speaks to you in Hindi in two or more turns in a row, switch to "
    "Hindi; switch back to English as soon as he speaks English again. A single Hindi word or phrase is not a "
    "reason to switch. {gender} If he says goodbye, say a short goodbye."
)
FEMALE_VOICES = {"tiffany", "amy", "kiara", "lupe", "carolina", "ambre", "beatrice", "tina", "olivia"}


def talk_prompt(voice: str) -> str:
    gender = ("You are female: in Hindi always use feminine first-person forms (मैं कर सकती हूँ, मैं अच्छी हूँ)."
              if voice in FEMALE_VOICES else
              "You are male: in Hindi always use masculine first-person forms (मैं कर सकता हूँ, मैं अच्छा हूँ).")
    return TALK_PROMPT.format(gender=gender)
MAX_CALL_SECONDS = 5 * 60
SILENT_HANGUP_SECONDS = 10


async def talk(settings: Settings, *, voice: str | None = None, prompt: str | None = None, greeting: str | None =
               "The call just connected. Greet Ankur in English in one short sentence and ask how he's doing.") -> list:
    """Ring the owner and hold a live conversation with Nova 2 Sonic until someone hangs up."""
    from .sonic import Sonic

    user_id = settings.owner_telegram_user_id
    caller = Caller(settings)
    await caller.start()
    out = bytearray()                       # 48 kHz audio waiting to be played
    inq: asyncio.Queue[bytes] = asyncio.Queue()
    voice = voice or settings.tg_call_voice
    print(f"Voice: {voice}", flush=True)
    sonic = Sonic(system_prompt=prompt or talk_prompt(voice), voice=voice, region=settings.aws_region,
                  on_audio=lambda pcm: out.extend(up_from_24k(pcm)),
                  on_interrupt=out.clear,
                  on_text=lambda role, text: print(f"{'Ankur' if role == 'USER' else 'ATYA'}: {text}", flush=True))
    try:
        print(f"Ringing {user_id}...", flush=True)
        try:
            await caller.ring(user_id)
        except Exception as e:
            print(f"Call not answered or failed: {type(e).__name__}: {e}")
            return []
        try:
            await sonic.open()
        except Exception as e:
            print(f"Couldn't reach the voice model: {type(e).__name__}: {e}", flush=True)
            await caller.hang_up(user_id)
            return []
        caller.on_audio = inq.put_nowait
        if greeting:
            await sonic.say_first(greeting)
        print("Connected. Talk; hang up to end.", flush=True)

        async def play():
            from pytgcalls.types import Device
            start, n = time.monotonic(), 0
            while not caller.ended.is_set() and not sonic.done.is_set():
                frame = bytes(out[:FRAME_BYTES]) if len(out) >= FRAME_BYTES else bytes(FRAME_BYTES)
                del out[:FRAME_BYTES]
                await caller.calls.send_frame(user_id, Device.MICROPHONE, frame)
                n += 1
                ahead = start + n * FRAME_MS / 1000 - time.monotonic()
                if ahead > 0:
                    await asyncio.sleep(ahead)
                elif ahead < -0.2:                 # fell behind (e.g. a pause): don't rush to catch up
                    start, n = time.monotonic(), 0

        async def listen():
            pending = bytearray()
            while not caller.ended.is_set() and not sonic.done.is_set():
                try:
                    pending.extend(await asyncio.wait_for(inq.get(), timeout=0.5))
                except asyncio.TimeoutError:
                    continue
                if len(pending) >= 3072 * 3:          # ~32 ms of audio per event
                    await sonic.send_audio(down_to_16k(bytes(pending)))
                    pending.clear()

        async def watchdog():
            # Telegram doesn't always tell us the other side hung up; their audio stopping does.
            caller.last_heard = time.monotonic()
            while not caller.ended.is_set():
                await asyncio.sleep(1)
                if time.monotonic() - caller.last_heard > SILENT_HANGUP_SECONDS:
                    log.info("no audio from the call for %ss: treating it as hung up", SILENT_HANGUP_SECONDS)
                    caller.ended.set()

        tasks = [asyncio.create_task(play()), asyncio.create_task(listen()), asyncio.create_task(watchdog())]
        ended = asyncio.create_task(caller.ended.wait())
        stopped = asyncio.create_task(sonic.done.wait())
        await asyncio.wait([ended, stopped], timeout=MAX_CALL_SECONDS, return_when=asyncio.FIRST_COMPLETED)
        if sonic.error:
            print(f"Voice model error: {sonic.error}")
        print(f"Model sent {sonic.events} events, {sonic.audio_out / 2 / 24000:.1f} s of speech.", flush=True)
        for t in (*tasks, ended, stopped):
            t.cancel()
        caller.on_audio = None
        await sonic.close()
        await caller.hang_up(user_id)
        return sonic.transcript
    finally:
        await caller.stop()


async def sonic_check(settings: Settings) -> int:
    """Talk to the voice model without a call: ask it to say hello and report what came back."""
    from .sonic import Sonic

    s = Sonic(system_prompt="You are a friendly assistant. Answer in one short sentence.",
              voice=settings.tg_call_voice, region=settings.aws_region,
              on_text=lambda role, text: print(f"{role}: {text}", flush=True))
    try:
        await s.open()
    except Exception as e:
        print(f"FAILED to open the voice model: {type(e).__name__}: {e}")
        return 1
    await s.say_first("Say hello and tell me today's weekday is a good day.")
    for _ in range(40):                                   # up to 20 s, feeding silence like a quiet line
        await s.send_audio(bytes(16000 * 2 // 2))         # 0.5 s of silence at 16 kHz
        await asyncio.sleep(0.5)
        if s.done.is_set() or (s.audio_out and any(r == "ASSISTANT" for r, _ in s.transcript)):
            break
    await s.close()
    print(f"events: {s.events}, speech: {s.audio_out / 2 / 24000:.1f} s, error: {s.error or 'none'}")
    return 0 if s.audio_out else 1


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    if argv[:1] == ["test"]:
        return asyncio.run(ring_test(load_settings()))
    if argv[:1] == ["check"]:
        return asyncio.run(sonic_check(load_settings()))
    if argv[:1] == ["talk"]:
        transcript = asyncio.run(talk(load_settings(), voice=argv[1].lower() if len(argv) > 1 else None))
        print(f"\nCall over: {sum(1 for r, _ in transcript if r == 'USER')} things heard from you.")
        return 0 if transcript else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
