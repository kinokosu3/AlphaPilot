"""Reusable OCR provider contract and lazy provider registry.

Providers translate a local PDF into the reader-neutral ``PDFReadResult``
contract.  Provider-specific credentials, SDK imports, retries and response
mapping belong in the provider implementation, not in the report-factor
service.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, ClassVar

from alphapilot.modules.report_factor.readers.base import PageText, PDFReadResult

OCR_PROVIDER_ENTRY_POINT_GROUP = "alphapilot.report_factor.ocr_providers"
RESERVED_OCR_MODES = frozenset({"auto", "local"})
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def normalize_provider_id(value: str) -> str:
    """Normalize and validate a stable, user-facing OCR provider id."""
    provider_id = str(value or "").strip().lower()
    if not _PROVIDER_ID_PATTERN.fullmatch(provider_id):
        raise ValueError(
            "OCR provider id must start with a letter and contain only "
            "lowercase letters, digits, '_' or '-' (maximum 64 characters)"
        )
    if provider_id in RESERVED_OCR_MODES:
        raise ValueError(f"OCR provider id {provider_id!r} is reserved")
    return provider_id


class OCRProvider(ABC):
    """Abstract contract implemented by every remote or local OCR adapter.

    Implementations should delay importing heavy vendor SDKs until
    :meth:`extract_pdf` is called.  They must preserve PDF page numbers and
    return ``ocr_used=True``.
    """

    provider_id: ClassVar[str]
    display_name: ClassVar[str] = ""

    @abstractmethod
    def extract_pdf(self, source: Path) -> PDFReadResult:
        """OCR one local PDF and return page-preserving normalized text."""
        raise NotImplementedError


OCRProviderFactory = Callable[[], OCRProvider]


@dataclass(frozen=True)
class OCRProviderRegistration:
    """Public, secret-free metadata for one lazily-created provider."""

    provider_id: str
    display_name: str
    factory: OCRProviderFactory
    source: str = "manual"

    def to_dict(self) -> dict[str, str]:
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "source": self.source,
        }


class OCRProviderRegistry:
    """Thread-safe registry that resolves OCR adapters only when selected."""

    def __init__(self) -> None:
        self._providers: dict[str, OCRProviderRegistration] = {}
        self._lock = RLock()

    def register(
        self,
        provider_id: str,
        factory: OCRProviderFactory,
        *,
        display_name: str | None = None,
        source: str = "manual",
        replace: bool = False,
    ) -> None:
        """Register a zero-argument factory without constructing the provider."""
        normalized = normalize_provider_id(provider_id)
        if not callable(factory):
            raise TypeError("OCR provider factory must be callable")
        registration = OCRProviderRegistration(
            provider_id=normalized,
            display_name=str(display_name or normalized).strip() or normalized,
            factory=factory,
            source=str(source or "manual"),
        )
        with self._lock:
            if normalized in self._providers and not replace:
                raise ValueError(f"OCR provider {normalized!r} is already registered")
            self._providers[normalized] = registration

    def has(self, provider_id: str) -> bool:
        try:
            normalized = normalize_provider_id(provider_id)
        except ValueError:
            return False
        with self._lock:
            return normalized in self._providers

    def registrations(self) -> list[OCRProviderRegistration]:
        with self._lock:
            return [self._providers[name] for name in sorted(self._providers)]

    def provider_ids(self) -> list[str]:
        return [item.provider_id for item in self.registrations()]

    def create(self, provider_id: str) -> OCRProvider:
        normalized = normalize_provider_id(provider_id)
        with self._lock:
            registration = self._providers.get(normalized)
        if registration is None:
            available = ", ".join(self.provider_ids()) or "none"
            raise ValueError(
                f"Unknown OCR provider {normalized!r}. Available providers: {available}"
            )
        provider = registration.factory()
        if not isinstance(provider, OCRProvider):
            raise TypeError(
                f"OCR provider factory {normalized!r} returned "
                f"{type(provider).__name__}; expected OCRProvider"
            )
        actual_id = normalize_provider_id(getattr(provider, "provider_id", ""))
        if actual_id != normalized:
            raise ValueError(
                f"OCR provider registration id {normalized!r} does not match "
                f"implementation id {actual_id!r}"
            )
        return provider

    def extract_pdf(self, provider_id: str, source: Path) -> PDFReadResult:
        """Resolve one provider and enforce the normalized result contract."""
        normalized = normalize_provider_id(provider_id)
        result = self.create(normalized).extract_pdf(source)
        return _validate_ocr_result(normalized, result)


def _validate_ocr_result(provider_id: str, result: Any) -> PDFReadResult:
    if not isinstance(result, PDFReadResult):
        raise TypeError(
            f"OCR provider {provider_id!r} returned {type(result).__name__}; "
            "expected PDFReadResult"
        )
    if not result.ocr_used:
        raise ValueError(f"OCR provider {provider_id!r} must return ocr_used=True")
    if not str(result.parser or "").strip():
        raise ValueError(f"OCR provider {provider_id!r} returned an empty parser name")
    if not result.pages:
        raise ValueError(f"OCR provider {provider_id!r} returned no pages")

    page_numbers: set[int] = set()
    for page in result.pages:
        if not isinstance(page, PageText):
            raise TypeError(
                f"OCR provider {provider_id!r} returned a non-PageText page: "
                f"{type(page).__name__}"
            )
        if not isinstance(page.page_number, int) or isinstance(page.page_number, bool):
            raise TypeError(
                f"OCR provider {provider_id!r} returned a non-integer page number: "
                f"{page.page_number!r}"
            )
        if page.page_number < 1:
            raise ValueError(
                f"OCR provider {provider_id!r} returned invalid page number "
                f"{page.page_number}"
            )
        if page.page_number in page_numbers:
            raise ValueError(
                f"OCR provider {provider_id!r} returned duplicate page number "
                f"{page.page_number}"
            )
        page_numbers.add(page.page_number)
        if not isinstance(page.text, str):
            raise TypeError(
                f"OCR provider {provider_id!r} page {page.page_number} text must be str"
            )
    if result.text_chars == 0:
        raise ValueError(f"OCR provider {provider_id!r} returned no readable text")
    return result


def _entry_points() -> list[Any]:
    from importlib.metadata import entry_points

    try:
        return list(entry_points(group=OCR_PROVIDER_ENTRY_POINT_GROUP))
    except TypeError:  # pragma: no cover - compatibility with older metadata API
        legacy = entry_points()
        return list(legacy.get(OCR_PROVIDER_ENTRY_POINT_GROUP, []))  # type: ignore[attr-defined]


def _entry_point_factory(entry_point: Any) -> OCRProvider:
    loaded = entry_point.load()
    if isinstance(loaded, OCRProvider):
        return loaded
    if callable(loaded):
        provider = loaded()
        if isinstance(provider, OCRProvider):
            return provider
        raise TypeError(
            f"OCR entry point {entry_point.name!r} returned "
            f"{type(provider).__name__}; expected OCRProvider"
        )
    raise TypeError(
        f"OCR entry point {entry_point.name!r} loaded {type(loaded).__name__}; "
        "expected an OCRProvider or zero-argument factory"
    )


def discover_ocr_providers(registry: OCRProviderRegistry) -> list[str]:
    """Register installed provider entry points without importing their SDKs.

    Entry points are loaded only if their provider is selected. Invalid names
    and duplicates are isolated so one third-party package cannot disable the
    built-in providers.
    """
    skipped: list[str] = []
    for entry_point in _entry_points():
        raw_name = str(getattr(entry_point, "name", "") or "")
        try:
            provider_id = normalize_provider_id(raw_name)
            if registry.has(provider_id):
                skipped.append(f"{provider_id}: duplicate provider id")
                continue
            registry.register(
                provider_id,
                lambda item=entry_point: _entry_point_factory(item),
                display_name=provider_id,
                source="entry_point",
            )
        except Exception as exc:  # noqa: BLE001 - isolate broken plugin metadata
            skipped.append(f"{raw_name or '<unnamed>'}: {type(exc).__name__}: {exc}")
    return skipped
