"""Kernel module for independent PDF research-factor extraction."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from alphapilot.kernel.base import BaseModule
from alphapilot.modules.report_factor.settings import ReportFactorSettings
from alphapilot.modules.report_factor.types import CommitFactorInput

if TYPE_CHECKING:
    from alphapilot.kernel.context import Context
    from alphapilot.modules.report_factor.readers.ocr import (
        OCRProviderFactory,
        OCRProviderRegistry,
    )


def _result_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return {"acceptable": False, "code": "unknown_result", "message": repr(value)}


class ReportFactorModule(BaseModule):
    """Extract reviewable factor drafts and explicitly commit selected drafts."""

    name = "report_factor"

    def setup(self, context: "Context") -> None:
        self.context = context
        self.settings = ReportFactorSettings.load()
        self._ocr_registry: "OCRProviderRegistry | None" = None
        self._ocr_registry_lock = RLock()

    def _get_ocr_registry(self) -> "OCRProviderRegistry":
        with self._ocr_registry_lock:
            if self._ocr_registry is None:
                from alphapilot.modules.report_factor.readers.providers import (
                    build_ocr_provider_registry,
                )

                self._ocr_registry = build_ocr_provider_registry(self.settings)
            return self._ocr_registry

    def register_ocr_provider(
        self,
        provider_id: str,
        factory: "OCRProviderFactory",
        *,
        display_name: str | None = None,
        replace: bool = False,
    ) -> None:
        """Register a provider factory for direct/in-process module usage.

        Portal job workers start in a fresh process. Providers intended for
        Portal jobs should also publish the
        ``alphapilot.report_factor.ocr_providers`` Python entry point.
        """
        self._get_ocr_registry().register(
            provider_id,
            factory,
            display_name=display_name,
            source="manual",
            replace=replace,
        )

    def ocr_providers(self) -> dict[str, Any]:
        """Return secret-free OCR capability metadata for API/UI discovery."""
        registry = self._get_ocr_registry()
        return {
            "default_provider": self.settings.default_ocr_provider,
            "modes": ["auto", "local", *registry.provider_ids()],
            "providers": [item.to_dict() for item in registry.registrations()],
        }

    def validate_ocr_mode(self, ocr_mode: str) -> str:
        """Validate a request mode without constructing any provider or SDK client."""
        normalized = str(ocr_mode or "").strip().lower()
        if normalized in {"auto", "local"}:
            return normalized
        from alphapilot.modules.report_factor.readers.ocr import normalize_provider_id

        provider_id = normalize_provider_id(normalized)
        registry = self._get_ocr_registry()
        if not registry.has(provider_id):
            available = ", ".join(["auto", "local", *registry.provider_ids()])
            raise ValueError(
                f"Unknown OCR mode/provider {provider_id!r}. Available modes: {available}"
            )
        return provider_id

    def extract_pdf(
        self,
        source: str,
        *,
        market: str = "China A-share",
        frequency: str = "daily",
        available_data: list[str] | None = None,
        ocr_mode: str = "auto",
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        # Service import is intentionally delayed so engine/live startup does not
        # initialize PDF, OCR, prompt or LLM dependencies.
        from alphapilot.modules.report_factor.service import ReportFactorService

        service = ReportFactorService(
            llm=self.context.get_llm(),
            factor_system=self.context.factor(),
            settings=self.settings,
            prompts_path=Path(__file__).with_name("prompts.yaml"),
            ocr_registry=self._get_ocr_registry(),
        )
        return service.extract(
            source,
            market=market,
            frequency=frequency,
            available_data=available_data,
            ocr_mode=ocr_mode,
            progress_callback=progress_callback,
        ).to_dict()

    def commit_factors(self, job_id: str, factors: list[dict[str, Any]]) -> dict[str, Any]:
        """Commit reviewed drafts; job-result membership is checked by the API boundary."""
        factor_system = self.context.factor()
        committed: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in factors:
            try:
                item = CommitFactorInput.model_validate(raw)
            except Exception as exc:  # noqa: BLE001 - return one structured rejection
                rejected.append(
                    {
                        "draft_id": str(raw.get("draft_id") or "") if isinstance(raw, dict) else "",
                        "acceptable": False,
                        "code": "invalid_commit_item",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if item.draft_id in seen:
                rejected.append(
                    {
                        "draft_id": item.draft_id,
                        "factor_name": item.factor_name,
                        "acceptable": False,
                        "code": "duplicate_draft_id",
                        "message": "The same draft was submitted more than once.",
                    }
                )
                continue
            seen.add(item.draft_id)
            result = factor_system.add_factor(
                item.factor_name,
                item.factor_expression,
                categories=item.categories,
            )
            payload = {
                "draft_id": item.draft_id,
                "factor_name": item.factor_name,
                **_result_dict(result),
            }
            (committed if payload.get("acceptable") else rejected).append(payload)
        return {
            "job_id": job_id,
            "committed": committed,
            "rejected": rejected,
            "n_requested": len(factors),
            "n_committed": len(committed),
            "n_rejected": len(rejected),
        }
