"""Photos and screenshots: checks and resizing for the vision model. No AI here.

A photo is read once by the model (LLM.read_image: what it shows, plus every bit of text
in it); the processor then treats caption + that reading like a typed message, so a
screenshot of a job post with "apply by tonight" becomes a task like any other message.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger("images")

MAX_BYTES = 15 * 1024 * 1024       # what we accept from the chat apps
MODEL_BYTES = 3_500_000            # Bedrock's image limit is 3.75 MB
MAX_SIDE = 3000                    # plenty for reading screenshots and receipts
PLACEHOLDER = "[Photo]"            # the inbox text when a photo comes without a caption
FORMATS = {"image/jpeg": "jpeg", "image/jpg": "jpeg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


class ImageError(Exception):
    """A photo we can't use, with a message fit for the user."""


def is_image(mime: str, filename: str = "") -> bool:
    mime = (mime or "").split(";")[0].strip().lower()
    return mime in FORMATS or filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def mime_for(mime: str, filename: str = "") -> str:
    mime = (mime or "").split(";")[0].strip().lower()
    if mime in FORMATS:
        return "image/jpeg" if mime == "image/jpg" else mime
    ext = filename.lower().rsplit(".", 1)[-1]
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp",
            "gif": "image/gif"}.get(ext, "image/jpeg")


def check(data: bytes) -> None:
    if not data:
        raise ImageError("That photo came through empty. Try sending it again.")
    if len(data) > MAX_BYTES:
        raise ImageError(f"That photo is {len(data) // (1024 * 1024)} MB; the limit is 15 MB.")


def for_model(data: bytes, mime: str) -> tuple[bytes, str]:
    """(bytes, format) the vision model accepts: as is when small enough, else a resized JPEG."""
    fmt = FORMATS.get(mime, "jpeg")
    if len(data) <= MODEL_BYTES and fmt in ("jpeg", "png", "webp"):
        try:
            from PIL import Image
            with Image.open(io.BytesIO(data)) as im:
                if max(im.size) <= MAX_SIDE:
                    return data, fmt
        except Exception:
            return data, fmt                   # can't inspect it: let the model try
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        raise ImageError("I couldn't open that photo. Try sending it again, or as a screenshot.") from e
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    for quality in (85, 70, 55):
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=quality, optimize=True)
        if out.tell() <= MODEL_BYTES:
            return out.getvalue(), "jpeg"
        im.thumbnail((im.size[0] * 3 // 4, im.size[1] * 3 // 4))
    return out.getvalue(), "jpeg"


def as_message(caption: str, reading: dict) -> str:
    """What the rest of ATYA works on: the caption, then what the photo shows and the text in it."""
    parts = [caption.strip()] if caption.strip() else []
    if reading.get("description"):
        parts.append(f"{PLACEHOLDER} {reading['description'].strip()}")
    else:
        parts.append(PLACEHOLDER)
    if reading.get("text"):
        parts.append("Text in the photo:\n" + reading["text"].strip())
    return "\n\n".join(parts)
