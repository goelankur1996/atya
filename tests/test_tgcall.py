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
