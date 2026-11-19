"""A one-off test: your recent voice notes, transcribed by Voxtral (Bedrock) next to what Amazon
Transcribe heard. Reads and prints only; changes nothing.

    python -m atya.stt_compare [how many, default 10]

Only the owner's own voice notes are used.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time

MODELS = [("Voxtral Small", "mistral.voxtral-small-24b-2507"), ("Voxtral Mini", "mistral.voxtral-mini-3b-2507")]
PROMPT = ("Transcribe this voice note exactly, word for word. The speaker may mix Hindi and English: write Hindi in "
          "Latin script (romanised, the way people text it), keep English words in English, and keep names, numbers, "
          "dates and times exactly. Don't summarise, don't skip anything, don't add anything. Output only the "
          "transcript.")


def to_wav(data: bytes) -> bytes:
    """Any voice note (ogg/opus, m4a, mp3...) to 16 kHz mono WAV, which every model takes."""
    import av
    out = io.BytesIO()
    with av.open(io.BytesIO(data)) as src, av.open(out, "w", format="wav") as dst:
        stream = dst.add_stream("pcm_s16le", rate=16000)
        stream.layout = "mono"
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in src.decode(audio=0):
            for f in resampler.resample(frame):
                for p in stream.encode(f):
                    dst.mux(p)
        for p in stream.encode(None):
            dst.mux(p)
    return out.getvalue()


def ask(client, model: str, wav: bytes) -> str:
    """Try the request shapes Bedrock's Mistral models use for audio; return the first that works."""
    b64 = base64.b64encode(wav).decode()
    shapes = [
        [{"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}, {"type": "text", "text": PROMPT}],
        [{"type": "audio_url", "audio_url": f"data:audio/wav;base64,{b64}"}, {"type": "text", "text": PROMPT}],
    ]
    errors = []
    for content in shapes:
        body = {"messages": [{"role": "user", "content": content}], "max_tokens": 2000, "temperature": 0}
        try:
            r = client.invoke_model(modelId=model, body=json.dumps(body))
            out = json.loads(r["body"].read())
            choices = out.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                text = msg.get("content")
                if isinstance(text, list):
                    text = " ".join(c.get("text", "") for c in text if isinstance(c, dict))
                return (text or "").strip()
            return json.dumps(out)[:500]
        except Exception as e:                      # try the next shape, report all if none works
            errors.append(f"{type(e).__name__}: {str(e)[:200]}")
    return "FAILED: " + " | ".join(errors)


def main() -> int:
    import boto3
    from .config import load_settings
    from .store import Store
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    settings = load_settings()
    store = Store(settings.database_url)
    with store.tx(system=True) as tx:
        owner = tx.tenant_by_telegram(settings.owner_telegram_user_id)
    if owner is None:
        print("No owner account found.")
        return 1
    with store.tx(owner["id"]) as tx:
        notes = tx._all("SELECT id, created_at, mime_type, seconds, text, data FROM attachments "
                        "WHERE tenant_id = %s AND mime_type LIKE 'audio/%%' AND data IS NOT NULL "
                        "ORDER BY created_at DESC LIMIT %s", (owner["id"], n))
    client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
    print(f"{len(notes)} voice notes (most recent first)\n")
    for i, note in enumerate(notes, 1):
        print("=" * 100)
        print(f"#{i}  {note['created_at']:%d %b %H:%M}  {note['seconds'] or '?'} s  {note['mime_type']}")
        print(f"\n[Amazon Transcribe, current]\n{note['text'] or '(none)'}")
        try:
            wav = to_wav(bytes(note["data"]))
        except Exception as e:
            print(f"\n(couldn't convert the audio: {e})\n")
            continue
        for label, model in MODELS:
            t0 = time.monotonic()
            text = ask(client, model, wav)
            print(f"\n[{label}, {time.monotonic() - t0:.1f} s]\n{text}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
