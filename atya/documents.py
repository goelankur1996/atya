"""PDF handling: checks, text extraction and chunking. No AI here.

The processor calls extract() and chunk(); when a PDF has almost no text
(a scan), it asks the model to read the pages instead (see LLM.read_document).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re

log = logging.getLogger("documents")

MAX_BYTES = 20 * 1024 * 1024        # Telegram's limit for files a bot can download
MAX_SCAN_PAGES = 50                 # scans are read by the model; cap the cost
MAX_TEXT_CHARS = 400_000            # stored text per file (about 150 pages of prose)
CHUNK_CHARS = 1500
SCAN_CHARS_PER_PAGE = 25            # below this on average, treat the PDF as a scan


class DocumentError(Exception):
    """A file we can't use, with a message fit for the user."""


def is_pdf(data: bytes, filename: str = "", mime: str = "") -> bool:
    return data[:5] == b"%PDF-" or (mime == "application/pdf" and data[:1024].find(b"%PDF-") != -1)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check(data: bytes, filename: str = "", mime: str = "") -> None:
    if len(data) > MAX_BYTES:
        raise DocumentError(f"That file is {len(data) // (1024 * 1024)} MB; the limit is 20 MB.")
    if not data or not is_pdf(data, filename, mime):
        raise DocumentError("Only PDF files can be saved for now.")


def extract(data: bytes) -> list[str]:
    """Text of each page ('' for pages without a text layer)."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")
            except Exception:
                ok = 0
            if not ok:
                raise DocumentError("That PDF is password-protected. Send an unlocked copy.")
        pages = []
        for page in reader.pages:
            try:
                pages.append(_clean(page.extract_text() or ""))
            except Exception:                       # one broken page shouldn't sink the file
                pages.append("")
        return pages
    except DocumentError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as e:
        raise DocumentError("I couldn't open that PDF; it may be damaged.") from e


def _clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def looks_scanned(pages: list[str]) -> bool:
    if not pages:
        return True
    return sum(len(p) for p in pages) / len(pages) < SCAN_CHARS_PER_PAGE


def joined(pages: list[str]) -> str:
    """All page text with page markers, capped."""
    out = "\n\n".join(f"[page {i}]\n{p}" for i, p in enumerate(pages, 1) if p)
    return out[:MAX_TEXT_CHARS]


def chunk(pages: list[str], size: int = CHUNK_CHARS) -> list[tuple[int, str]]:
    """(page number, text) pieces of about `size` characters, split on paragraphs."""
    out: list[tuple[int, str]] = []
    total = 0
    for pno, text in enumerate(pages, 1):
        buf = ""
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if not para:
                continue
            while len(para) > size:                       # very long paragraph: hard split
                if buf:
                    out.append((pno, buf))
                    buf = ""
                out.append((pno, para[:size]))
                para = para[size:]
            if len(buf) + len(para) + 2 > size and buf:
                out.append((pno, buf))
                buf = ""
            buf = f"{buf}\n\n{para}" if buf else para
        if buf:
            out.append((pno, buf))
        total += len(text)
        if total > MAX_TEXT_CHARS:
            break
    return out


def text_chunks(text: str, size: int = CHUNK_CHARS) -> list[tuple[int | None, str]]:
    """Chunks of free text (a model's reading of a scan) without page numbers."""
    return [(None, c) for _, c in chunk([text], size)]
