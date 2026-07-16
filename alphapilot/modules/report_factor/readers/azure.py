"""Azure Document Intelligence OCR reader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from alphapilot.modules.report_factor.readers.base import PageText, PDFReadResult
from alphapilot.modules.report_factor.readers.ocr import OCRProvider


@dataclass(frozen=True)
class AzureDocumentIntelligenceOCRProvider(OCRProvider):
    """Built-in Azure adapter implementing the generic OCR contract."""

    provider_id: ClassVar[str] = "azure"
    display_name: ClassVar[str] = "Azure Document Intelligence"

    endpoint: str
    key: str

    def extract_pdf(self, source: Path) -> PDFReadResult:
        if not self.endpoint or not self.key:
            raise RuntimeError(
                "Azure OCR is required but credentials are not configured. Set "
                "ALPHAPILOT_REPORT_FACTOR_AZURE_KEY and "
                "ALPHAPILOT_REPORT_FACTOR_AZURE_ENDPOINT."
            )

        # Delayed imports keep Azure optional for ordinary engine/live startup.
        from azure.ai.formrecognizer import DocumentAnalysisClient
        from azure.core.credentials import AzureKeyCredential

        client = DocumentAnalysisClient(
            endpoint=self.endpoint,
            credential=AzureKeyCredential(self.key),
        )
        with source.open("rb") as handle:
            result = client.begin_analyze_document("prebuilt-layout", handle).result()

        pages: list[PageText] = []
        for index, page in enumerate(result.pages or [], start=1):
            lines = [str(line.content) for line in (getattr(page, "lines", None) or [])]
            pages.append(
                PageText(
                    page_number=int(getattr(page, "page_number", index) or index),
                    text="\n".join(lines),
                )
            )
        if not pages:
            content = str(getattr(result, "content", "") or "")
            if content:
                pages = [PageText(page_number=1, text=content)]
        if not pages:
            raise ValueError("Azure OCR returned no readable pages")
        return PDFReadResult(
            pages=pages,
            parser="azure_document_intelligence",
            ocr_used=True,
        )


def read_pdf_with_azure(path: Path, *, endpoint: str, key: str) -> PDFReadResult:
    """Compatibility wrapper around the provider implementation."""
    return AzureDocumentIntelligenceOCRProvider(endpoint=endpoint, key=key).extract_pdf(path)
