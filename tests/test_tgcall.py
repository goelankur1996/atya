"""The audio helpers behind the call test (the call itself needs a real Telegram account)."""
from atya import tgcall
from atya.config import Settings


def test_frames_are_10ms_and_padded():
    pcm = tgcall.tone(25)
    fs = tgcall.frames(pcm)
    assert len(fs) == 3 and all(len(f) == tgcall.FRAME_BYTES == 960 for f in fs)
    assert fs[-1].endswith(bytes(100))


def test_levels():
    assert tgcall.levels(tgcall.silence(100)) == (0.0, 0.0)
    peak, rms = tgcall.levels(tgcall.tone(100, volume=0.5))
    assert 0.48 < peak <= 0.5 and 0.3 < rms < 0.37


def test_off_until_all_three_are_set():
    s = Settings(database_url="x", telegram_bot_token="x", anthropic_api_key="x", owner_telegram_user_id=1,
                 owner_timezone="UTC", tg_caller_api_id="123", tg_caller_api_hash="h")
    assert not s.tg_caller_on
    try:
        tgcall.Caller(s)
    except RuntimeError as e:
        assert "TG_CALLER_SESSION" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_resampling_keeps_duration_and_shape():
    pcm48 = tgcall.tone(100, hz=440, volume=0.5)
    pcm16 = tgcall.down_to_16k(pcm48)
    assert len(pcm16) == len(pcm48) // 3
    assert 0.45 < tgcall.levels(pcm16)[0] <= 0.5
    pcm24 = pcm16 + pcm16[: len(pcm16) // 2]                 # 150 ms at 16k is 100 ms at 24k
    up = tgcall.up_from_24k(pcm24)
    assert len(up) == len(pcm24) * 2 and tgcall.up_from_24k(b"") == b""


def test_sonic_events_build_transcript_and_interrupts():
    import asyncio
    import base64
    from atya.sonic import Sonic

    heard, cut = [], []
    s = Sonic(system_prompt="x", on_audio=heard.append, on_interrupt=lambda: cut.append(1))
    events = [
        {"contentStart": {"role": "USER"}},
        {"textOutput": {"role": "USER", "content": "kal ka plan kya hai"}},
        {"contentStart": {"role": "ASSISTANT", "additionalModelFields": '{"generationStage":"SPECULATIVE"}'}},
        {"textOutput": {"role": "ASSISTANT", "content": "draft"}},
        {"contentStart": {"role": "ASSISTANT", "additionalModelFields": '{"generationStage":"FINAL"}'}},
        {"textOutput": {"role": "ASSISTANT", "content": "Two meetings tomorrow."}},
        {"audioOutput": {"content": base64.b64encode(b"\x01\x00" * 4).decode()}},
        {"textOutput": {"role": "ASSISTANT", "content": '{ "interrupted" : true }'}},
    ]

    async def go():
        for e in events:
            await s._handle(e)
    asyncio.run(go())
    assert s.transcript == [("USER", "kal ka plan kya hai"), ("ASSISTANT", "Two meetings tomorrow.")]
    assert heard == [b"\x01\x00" * 4] and cut == [1]
