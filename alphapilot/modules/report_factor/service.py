"""Side-effect-free PDF-to-factor-draft extraction pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml

from alphapilot.modules.report_factor.readers.base import PageText, PDFReadResult
from alphapilot.modules.report_factor.readers.ocr import (
    OCRProviderRegistry,
    normalize_provider_id,
)
from alphapilot.modules.report_factor.settings import ReportFactorSettings
from alphapilot.modules.report_factor.types import (
    FactorDraft,
    FactorValidation,
    FactorViability,
    ReportClassification,
    ReportFactorExtractionResult,
    ReportMetadata,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[..., None]

DEFAULT_AVAILABLE_DATA = [
    "daily OHLCV and turnover",
    "financial statements",
    "stock fundamentals",
    "minute OHLCV",
    "analyst consensus expectations",
]


def _emit_progress(
    callback: ProgressCallback | None,
    percent: int,
    stage: str,
    message: str,
    **details: Any,
) -> None:
    if callback is not None:
        callback(percent, stage, message, **details)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_factor_name(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()
    normalized = re.sub(r"_+", "_", text)
    if normalized:
        return normalized
    suffix = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:8]
    return f"factor_{suffix}"


def _clean_json_text(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start : end + 1]
        raise


class ReportFactorService:
    """Extract reviewable factor drafts without mutating any AlphaPilot system."""

    def __init__(
        self,
        *,
        llm: Any,
        factor_system: Any,
        settings: ReportFactorSettings,
        prompts_path: Path | None = None,
        ocr_registry: OCRProviderRegistry | None = None,
    ) -> None:
        self.llm = llm
        self.factor_system = factor_system
        self.settings = settings
        prompt_file = prompts_path or Path(__file__).with_name("prompts.yaml")
        payload = yaml.safe_load(prompt_file.read_text(encoding="utf-8")) or {}
        self.prompts: dict[str, str] = {str(k): str(v) for k, v in payload.items()}
        if ocr_registry is None:
            from alphapilot.modules.report_factor.readers.providers import (
                build_ocr_provider_registry,
            )

            ocr_registry = build_ocr_provider_registry(settings)
        self.ocr_registry = ocr_registry

    def extract(
        self,
        source: str,
        *,
        market: str = "China A-share",
        frequency: str = "daily",
        available_data: list[str] | None = None,
        ocr_mode: str = "auto",
        progress_callback: ProgressCallback | None = None,
    ) -> ReportFactorExtractionResult:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"PDF file not found: {path}")
        if path.suffix.lower() != ".pdf":
            raise ValueError("Report source must be a single .pdf file")
        ocr_mode = self.validate_ocr_mode(ocr_mode)

        report_hash = _sha256(path)
        logger.info("report-factor extraction started file=%s sha256=%s", path.name, report_hash)
        _emit_progress(progress_callback, 5, "parse", "Reading PDF text layer")
        read_result = self._read(path, ocr_mode=ocr_mode, progress_callback=progress_callback)
        if read_result.text_chars == 0:
            raise ValueError("PDF contains no readable text")

        chunks = self._build_chunks(read_result.pages)
        if not chunks:
            raise ValueError("PDF contains no non-empty report pages")
        warnings = list(read_result.warnings)
        _emit_progress(
            progress_callback,
            25,
            "extract",
            f"Extracting factors from {len(chunks)} page chunk(s)",
            total=len(chunks),
            completed=0,
        )

        classification = ReportClassification()
        summary = ""
        try:
            meta = self._chat_json(
                self.prompts["classify"],
                self._representative_report_text(chunks),
            )
            summary = str(meta.get("summary") or "").strip()
            raw_class = meta.get("classification")
            if isinstance(raw_class, dict):
                classification = ReportClassification.model_validate(raw_class)
        except Exception as exc:  # noqa: BLE001 - advisory metadata must not block extraction
            warnings.append(f"Report classification unavailable: {type(exc).__name__}: {exc}")

        context_data = available_data or list(DEFAULT_AVAILABLE_DATA)
        extraction_prompt = (
            self.prompts["extract"]
            .replace("{{ market }}", str(market))
            .replace("{{ frequency }}", str(frequency))
            .replace("{{ available_data }}", ", ".join(map(str, context_data)))
        )

        candidates: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks, start=1):
            try:
                result = self._chat_json(extraction_prompt, chunk["text"])
                raw_factors = result.get("factors", [])
                if not isinstance(raw_factors, list):
                    raise ValueError("LLM field 'factors' must be a list")
                for raw in raw_factors:
                    if isinstance(raw, dict):
                        parsed = self._parse_candidate(raw, allowed_pages=chunk["pages"])
                        if parsed is not None:
                            candidates.append(parsed)
            except Exception as exc:  # noqa: BLE001 - keep partial results from other chunks
                pages = ",".join(map(str, chunk["pages"]))
                warnings.append(
                    f"Factor extraction failed for pages {pages}: {type(exc).__name__}: {exc}"
                )
            percent = 25 + int(35 * index / len(chunks))
            _emit_progress(
                progress_callback,
                percent,
                "extract",
                f"Processed report chunk {index}/{len(chunks)}",
                total=len(chunks),
                completed=index,
            )

        merged = self._merge_candidates(candidates, report_hash=report_hash)
        _emit_progress(
            progress_callback,
            65,
            "translate",
            f"Translating {len(merged)} factor formula(s) to AlphaPilot DSL",
            total=len(merged),
            completed=0,
        )
        translation_warnings = self._translate(merged, context_data=context_data)
        warnings.extend(translation_warnings)

        _emit_progress(
            progress_callback,
            85,
            "validate",
            f"Validating {len(merged)} candidate expression(s)",
            total=len(merged),
            completed=0,
        )
        drafts: list[FactorDraft] = []
        for index, candidate in enumerate(merged, start=1):
            candidate["validation"] = self._validate_expression(
                candidate.get("factor_expression")
            ).to_dict()
            drafts.append(FactorDraft.model_validate(candidate))
            _emit_progress(
                progress_callback,
                85 + int(14 * index / max(len(merged), 1)),
                "validate",
                f"Validated factor {index}/{len(merged)}",
                total=len(merged),
                completed=index,
            )

        report = ReportMetadata(
            file_name=path.name,
            sha256=report_hash,
            page_count=len(read_result.pages),
            parser=read_result.parser,
            ocr_used=read_result.ocr_used,
            classification=classification,
        )
        result = ReportFactorExtractionResult(
            report=report,
            summary=summary,
            factors=drafts,
            warnings=warnings,
        )
        _emit_progress(
            progress_callback,
            100,
            "done",
            f"Extracted {len(drafts)} factor draft(s)",
            total=len(drafts),
            completed=len(drafts),
        )
        logger.info(
            "report-factor extraction completed file=%s sha256=%s factors=%d",
            path.name,
            report_hash,
            len(drafts),
        )
        return result

    def validate_ocr_mode(self, ocr_mode: str) -> str:
        """Validate a built-in mode or registered provider id."""
        normalized = str(ocr_mode or "").strip().lower()
        if normalized in {"auto", "local"}:
            return normalized
        provider_id = normalize_provider_id(normalized)
        if not self.ocr_registry.has(provider_id):
            available = ", ".join(["auto", "local", *self.ocr_registry.provider_ids()])
            raise ValueError(
                f"Unknown OCR mode/provider {provider_id!r}. Available modes: {available}"
            )
        return provider_id

    def _read(
        self,
        path: Path,
        *,
        ocr_mode: str,
        progress_callback: ProgressCallback | None,
    ) -> PDFReadResult:
        if ocr_mode not in {"auto", "local"}:
            _emit_progress(
                progress_callback,
                10,
                "ocr",
                f"Running OCR provider: {ocr_mode}",
                provider=ocr_mode,
            )
            return self.ocr_registry.extract_pdf(ocr_mode, path)

        from alphapilot.modules.report_factor.readers.local import read_pdf_locally

        local_result = read_pdf_locally(path)
        if ocr_mode == "local" or local_result.text_chars >= self.settings.min_text_chars:
            if local_result.text_chars < self.settings.min_text_chars:
                local_result.warnings.append(
                    "PDF text layer is sparse; OCR was disabled by ocr_mode=local."
                )
            return local_result

        provider_id = normalize_provider_id(self.settings.default_ocr_provider)
        _emit_progress(
            progress_callback,
            12,
            "ocr",
            f"PDF text layer is sparse; falling back to OCR provider: {provider_id}",
            provider=provider_id,
        )
        ocr_result = self.ocr_registry.extract_pdf(provider_id, path)
        ocr_result.warnings = [
            *local_result.warnings,
            f"Sparse PDF text layer was replaced by OCR provider {provider_id!r}.",
            *ocr_result.warnings,
        ]
        return ocr_result

    def _estimate_tokens(self, text: str, system_prompt: str = "") -> int:
        try:
            counted = int(
                self.llm.count_tokens(user_prompt=text, system_prompt=system_prompt) or 0
            )
        except Exception:  # noqa: BLE001 - character fallback is deterministic
            counted = 0
        return counted if counted > 0 else max(1, len(text) // 4)

    def _build_chunks(self, pages: Iterable[PageText]) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        current_text: list[str] = []
        current_pages: list[int] = []
        for page in pages:
            text = page.text.strip()
            if not text:
                continue
            fragments = [text]
            # A text-heavy table page can itself exceed a model limit. Split it
            # deterministically while retaining the same source page citation.
            while any(
                len(fragment) > 1
                and self._estimate_tokens(fragment) > self.settings.chunk_token_limit
                for fragment in fragments
            ):
                next_fragments: list[str] = []
                for fragment in fragments:
                    if (
                        len(fragment) <= 1
                        or self._estimate_tokens(fragment) <= self.settings.chunk_token_limit
                    ):
                        next_fragments.append(fragment)
                        continue
                    midpoint = len(fragment) // 2
                    split_at = fragment.rfind("\n", 0, midpoint)
                    if split_at < max(1, midpoint // 2):
                        split_at = midpoint
                    next_fragments.extend([fragment[:split_at], fragment[split_at:]])
                fragments = [fragment.strip() for fragment in next_fragments if fragment.strip()]

            for fragment_index, fragment in enumerate(fragments, start=1):
                part = f" PART {fragment_index}/{len(fragments)}" if len(fragments) > 1 else ""
                marked = f"[PAGE {page.page_number}{part}]\n{fragment}"
                candidate = "\n\n".join([*current_text, marked])
                if current_text and self._estimate_tokens(candidate) > self.settings.chunk_token_limit:
                    chunks.append({"text": "\n\n".join(current_text), "pages": current_pages})
                    current_text = []
                    current_pages = []
                current_text.append(marked)
                if page.page_number not in current_pages:
                    current_pages.append(page.page_number)
        if current_text:
            chunks.append({"text": "\n\n".join(current_text), "pages": current_pages})
        return chunks

    def _representative_report_text(self, chunks: list[dict[str, Any]]) -> str:
        """Sample every report chunk while staying below a conservative budget."""
        if len(chunks) == 1:
            return str(chunks[0]["text"])
        remaining_chars = self.settings.chunk_token_limit * 3
        samples: list[str] = []
        for index, chunk in enumerate(chunks):
            chunks_left = len(chunks) - index
            quota = max(1, remaining_chars // chunks_left)
            sample = str(chunk["text"])[:quota]
            samples.append(sample)
            remaining_chars -= len(sample)
        return "\n\n".join(samples)

    def _chat_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for _attempt in range(self.settings.llm_json_retries + 1):
            try:
                response = self.llm.chat_completion(
                    user_prompt=user_prompt,
                    system_prompt=system_prompt,
                    json_mode=True,
                )
                parsed = json.loads(_clean_json_text(str(response)))
                if not isinstance(parsed, dict):
                    raise ValueError("LLM response must be a JSON object")
                return parsed
            except Exception as exc:  # noqa: BLE001 - retry malformed provider output
                last_error = exc
        raise RuntimeError("LLM returned invalid JSON after configured retries") from last_error

    def _parse_candidate(
        self,
        raw: dict[str, Any],
        *,
        allowed_pages: list[int],
    ) -> dict[str, Any] | None:
        raw_name = str(raw.get("factor_name") or raw.get("name") or "").strip()
        if not raw_name:
            return None
        factor_name = _normalize_factor_name(raw_name)
        warnings: list[str] = []

        requested_pages: list[int] = []
        for value in raw.get("source_pages") or []:
            try:
                page = int(value)
            except (TypeError, ValueError):
                continue
            if page in allowed_pages and page not in requested_pages:
                requested_pages.append(page)
        if not requested_pages:
            requested_pages = list(allowed_pages)
            warnings.append("LLM did not provide valid page citations; chunk pages were retained.")

        raw_variables = raw.get("variables")
        variables = (
            {str(key): str(value) for key, value in raw_variables.items()}
            if isinstance(raw_variables, dict)
            else {}
        )
        evidence = [
            str(item).strip()[: self.settings.max_evidence_chars]
            for item in (raw.get("evidence") or [])
            if str(item).strip()
        ][: self.settings.max_evidence_items]

        raw_viability = raw.get("viability")
        if not isinstance(raw_viability, dict):
            raw_viability = {}
        status = str(raw_viability.get("status") or "unknown").lower()
        if status not in {"viable", "unviable", "unknown"}:
            status = "unknown"

        return {
            "factor_name": factor_name,
            "description": str(raw.get("description") or "").strip(),
            "formulation": str(raw.get("formulation") or "").strip(),
            "variables": variables,
            "source_pages": requested_pages,
            "evidence": evidence,
            "viability": {
                "status": status,
                "reason": str(raw_viability.get("reason") or "").strip(),
            },
            "warnings": warnings,
        }

    def _merge_candidates(
        self,
        candidates: list[dict[str, Any]],
        *,
        report_hash: str,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for item in candidates:
            key = _normalize_factor_name(str(item["factor_name"]))
            if key not in merged:
                merged[key] = dict(item)
                continue
            current = merged[key]
            if len(str(item.get("description") or "")) > len(str(current.get("description") or "")):
                current["description"] = item["description"]
            if len(str(item.get("formulation") or "")) > len(str(current.get("formulation") or "")):
                current["formulation"] = item["formulation"]
            current["variables"] = {**current.get("variables", {}), **item.get("variables", {})}
            current["source_pages"] = sorted(
                set(current.get("source_pages", [])) | set(item.get("source_pages", []))
            )
            current["evidence"] = list(
                dict.fromkeys([*current.get("evidence", []), *item.get("evidence", [])])
            )[: self.settings.max_evidence_items]
            current["warnings"] = list(
                dict.fromkeys([*current.get("warnings", []), *item.get("warnings", [])])
            )
            statuses = {
                str(current.get("viability", {}).get("status", "unknown")),
                str(item.get("viability", {}).get("status", "unknown")),
            }
            if "viable" in statuses:
                current["viability"] = item["viability"] if item["viability"]["status"] == "viable" else current["viability"]
            elif "unknown" in statuses:
                current["viability"] = FactorViability().to_dict()

        output: list[dict[str, Any]] = []
        for key in sorted(merged):
            item = merged[key]
            item["draft_id"] = hashlib.sha256(
                f"{report_hash}:{key}".encode("utf-8")
            ).hexdigest()[:20]
            item["factor_expression"] = None
            output.append(item)
        return output

    def _translate(
        self,
        candidates: list[dict[str, Any]],
        *,
        context_data: list[str],
    ) -> list[str]:
        warnings: list[str] = []
        for start in range(0, len(candidates), 20):
            batch = candidates[start : start + 20]
            input_payload = {
                "available_data": context_data,
                "factors": [
                    {
                        "draft_id": item["draft_id"],
                        "factor_name": item["factor_name"],
                        "description": item["description"],
                        "formulation": item["formulation"],
                        "variables": item["variables"],
                    }
                    for item in batch
                ],
            }
            try:
                response = self._chat_json(
                    self.prompts["translate"],
                    json.dumps(input_payload, ensure_ascii=False),
                )
                expressions = response.get("expressions", [])
                if not isinstance(expressions, list):
                    raise ValueError("LLM field 'expressions' must be a list")
                by_id = {
                    str(item.get("draft_id")): item.get("factor_expression")
                    for item in expressions
                    if isinstance(item, dict) and item.get("draft_id")
                }
                for candidate in batch:
                    expression = by_id.get(candidate["draft_id"])
                    candidate["factor_expression"] = (
                        str(expression).strip() if expression is not None and str(expression).strip() else None
                    )
                    if candidate["factor_expression"] is None:
                        candidate["warnings"].append(
                            "No faithful AlphaPilot DSL expression was produced."
                        )
            except Exception as exc:  # noqa: BLE001 - drafts remain reviewable
                warnings.append(f"DSL translation unavailable: {type(exc).__name__}: {exc}")
                for candidate in batch:
                    candidate["warnings"].append("DSL translation failed; manual input is required.")
        return warnings

    def _validate_expression(self, expression: str | None) -> FactorValidation:
        if not expression:
            return FactorValidation()
        try:
            result = self.factor_system.validate_expression(expression)
            if hasattr(result, "to_dict"):
                payload = result.to_dict()
            elif hasattr(result, "__dict__"):
                payload = dict(result.__dict__)
            elif isinstance(result, dict):
                payload = result
            else:
                raise TypeError("Unsupported validation result")
            return FactorValidation.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - preserve draft and expose validation failure
            return FactorValidation(
                acceptable=False,
                code="validation_error",
                message=f"{type(exc).__name__}: {exc}",
            )
