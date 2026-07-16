"""Composition root for built-in and installed OCR providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from alphapilot.modules.report_factor.readers.ocr import (
    OCRProviderRegistry,
    discover_ocr_providers,
)

if TYPE_CHECKING:
    from alphapilot.modules.report_factor.readers.ocr import OCRProvider
    from alphapilot.modules.report_factor.settings import ReportFactorSettings


def build_ocr_provider_registry(
    settings: "ReportFactorSettings",
) -> OCRProviderRegistry:
    """Build a lazy registry without importing any vendor OCR SDK."""
    registry = OCRProviderRegistry()

    def azure_factory() -> "OCRProvider":
        from alphapilot.modules.report_factor.readers.azure import (
            AzureDocumentIntelligenceOCRProvider,
        )

        return AzureDocumentIntelligenceOCRProvider(
            endpoint=settings.azure_endpoint,
            key=settings.azure_key,
        )

    registry.register(
        "azure",
        azure_factory,
        display_name="Azure Document Intelligence",
        source="built_in",
    )
    discover_ocr_providers(registry)
    return registry
