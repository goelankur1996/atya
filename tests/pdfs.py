"""Tiny PDFs for tests: real text layers (Helvetica) or blank 'scanned' pages."""

from __future__ import annotations

import io


def text_pdf(pages: list[str]) -> bytes:
    objs: list[bytes] = []
    n = len(pages)
    # 1 catalog, 2 pages, 3 font, then (page, content) pairs
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        lines = []
        y = 760
        for line in text.split("\n"):
            safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"BT /F1 11 Tf 50 {y} Td ({safe}) Tj ET")
            y -= 16
        stream = "\n".join(lines).encode("latin-1", "replace")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def scanned_pdf(n_pages: int) -> bytes:
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(n_pages):
        w.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()
