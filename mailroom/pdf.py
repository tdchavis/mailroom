"""Pull readable text out of PDF attachments."""

from __future__ import annotations

import io

from pypdf import PdfReader

MAX_PAGES = 60


def pdf_text(data: bytes, max_pages: int = MAX_PAGES) -> str:
    """Return the text of a PDF, page by page. Scanned (image-only) pages come back empty,
    and we say so instead of pretending."""
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001 - any parse failure is reported, not raised
        return f"[Could not open this PDF: {e}]"
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:  # noqa: BLE001
            return "[This PDF is password-protected. Open it yourself.]"
    pages = []
    total = len(reader.pages)
    for i, page in enumerate(reader.pages[:max_pages], start=1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001
            text = ""
        pages.append(f"--- page {i} of {total} ---\n{text or '[no text on this page: likely a scan or image]'}")
    if total > max_pages:
        pages.append(f"[stopped after {max_pages} of {total} pages]")
    return "\n\n".join(pages)


def looks_scanned(text: str) -> bool:
    return text.count("[no text on this page") > 0 and len(text.replace(" ", "")) < 400
