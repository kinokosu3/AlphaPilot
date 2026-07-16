"""Page-preserving PDF readers for report-factor extraction."""

from alphapilot.modules.report_factor.readers.base import PageText, PDFReadResult
from alphapilot.modules.report_factor.readers.ocr import (
    OCR_PROVIDER_ENTRY_POINT_GROUP,
    OCRProvider,
    OCRProviderFactory,
    OCRProviderRegistry,
)

__all__ = [
    "OCR_PROVIDER_ENTRY_POINT_GROUP",
    "OCRProvider",
    "OCRProviderFactory",
    "OCRProviderRegistry",
    "PDFReadResult",
    "PageText",
]
