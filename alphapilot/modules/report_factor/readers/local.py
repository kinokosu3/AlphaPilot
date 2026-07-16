"""Local text-layer PDF reader."""

from __future__ import annotations

from pathlib import Path

from alphapilot.modules.report_factor.readers.base import PageText, PDFReadResult


def read_pdf_locally(path: Path) -> PDFReadResult:
    # Delayed import keeps engine startup independent from PDF dependencies.
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise ValueError("Encrypted PDF cannot be opened without a password") from exc
        if not unlocked:
            raise ValueError("Encrypted PDF cannot be opened without a password")

    pages: list[PageText] = []
    warnings: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - preserve usable pages
            text = ""
            warnings.append(f"Page {index} text extraction failed: {type(exc).__name__}")
        pages.append(PageText(page_number=index, text=text))
    if not pages:
        raise ValueError("PDF contains no pages")
    return PDFReadResult(pages=pages, parser="pypdf", warnings=warnings)
