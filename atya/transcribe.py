"""Voice notes to text with Amazon Transcribe.

The audio goes to a temporary S3 key, a batch transcription job reads it
(English (India) or Hindi, detected automatically), and the audio copy and
the job are deleted afterwards. The original audio is kept in the database
as an attachment, like a PDF.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Protocol

import requests

log = logging.getLogger("transcribe")

MAX_BYTES = 16 * 1024 * 1024
MAX_SECONDS = 15 * 60
LANGUAGES = ["en-IN", "hi-IN"]
POLL_EVERY = 2.0
GIVE_UP_AFTER = 240            # seconds; stays under the processor's 5-minute reclaim
TMP_PREFIX = "transcribe-tmp/"

# MIME type -> (Transcribe media format, file extension)
FORMATS = {
    "audio/ogg": ("ogg", "ogg"), "audio/opus": ("ogg", "ogg"), "audio/oga": ("ogg", "ogg"),
    "audio/mpeg": ("mp3", "mp3"), "audio/mp3": ("mp3", "mp3"),
    "audio/mp4": ("mp4", "m4a"), "audio/x-m4a": ("mp4", "m4a"), "audio/m4a": ("mp4", "m4a"),
    "audio/amr": ("amr", "amr"), "audio/wav": ("wav", "wav"), "audio/x-wav": ("wav", "wav"),
    "audio/flac": ("flac", "flac"), "audio/webm": ("webm", "webm"),
}


class TranscribeError(Exception):
    pass


class Transcriber(Protocol):
    def transcribe(self, data: bytes, mime_type: str) -> dict: ...   # {"text", "language"}


def base_mime(mime_type: str) -> str:
    return (mime_type or "").split(";")[0].strip().lower()


def supported(mime_type: str) -> bool:
    return base_mime(mime_type) in FORMATS


def extension(mime_type: str) -> str:
    return FORMATS.get(base_mime(mime_type), ("", "ogg"))[1]


def clock(seconds: int | None) -> str:
    if not seconds:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def placeholder(seconds: int | None) -> str:
    """The inbox text for a voice note (inbox rows never change, so the
    transcript lives with the attachment)."""
    return f"[Voice note {clock(seconds)}]".replace(" ]", "]")


def chunks(text: str, size: int = 1500) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + len(w) + 1 > size:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        out.append(cur)
    return out


class AWSTranscriber:
    def __init__(self, *, bucket: str, region: str = "us-east-1", s3: Any = None, client: Any = None) -> None:
        if s3 is None or client is None:
            import boto3
            s3 = s3 or boto3.client("s3", region_name=region)
            client = client or boto3.client("transcribe", region_name=region)
        self.bucket, self.s3, self.client = bucket, s3, client

    def transcribe(self, data: bytes, mime_type: str) -> dict:
        if not supported(mime_type):
            raise TranscribeError(f"unsupported audio type {mime_type!r}")
        fmt, ext = FORMATS[base_mime(mime_type)]
        job = f"atya-{uuid.uuid4().hex}"
        key = f"{TMP_PREFIX}{job}.{ext}"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        try:
            self.client.start_transcription_job(
                TranscriptionJobName=job, Media={"MediaFileUri": f"s3://{self.bucket}/{key}"},
                MediaFormat=fmt, IdentifyLanguage=True, LanguageOptions=LANGUAGES)
            deadline = time.monotonic() + GIVE_UP_AFTER
            while True:
                j = self.client.get_transcription_job(TranscriptionJobName=job)["TranscriptionJob"]
                status = j["TranscriptionJobStatus"]
                if status == "COMPLETED":
                    break
                if status == "FAILED":
                    raise TranscribeError(j.get("FailureReason") or "transcription failed")
                if time.monotonic() > deadline:
                    raise TranscribeError("transcription took too long")
                time.sleep(POLL_EVERY)
            uri = j["Transcript"]["TranscriptFileUri"]
            r = requests.get(uri, timeout=30)
            r.raise_for_status()
            body = r.json()
            results = body.get("results", {})
            text = " ".join(t.get("transcript", "") for t in results.get("transcripts", [])).strip()
            ends = [float(i["end_time"]) for i in results.get("items", []) if i.get("end_time")]
            return {"text": text, "language": j.get("LanguageCode"),
                    "seconds": int(max(ends)) + 1 if ends else None}
        finally:
            for cleanup in (lambda: self.s3.delete_object(Bucket=self.bucket, Key=key),
                            lambda: self.client.delete_transcription_job(TranscriptionJobName=job)):
                try:
                    cleanup()
                except Exception as e:  # leftovers are harmless; note and move on
                    log.warning("cleanup after %s: %s", job, e)
