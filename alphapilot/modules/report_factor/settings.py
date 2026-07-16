"""Environment-backed settings owned by the report-factor module."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from alphapilot.kernel.paths import important_data_dir


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class ReportFactorSettings:
    upload_root: Path
    max_upload_bytes: int
    min_text_chars: int
    chunk_token_limit: int
    max_evidence_chars: int
    max_evidence_items: int
    llm_json_retries: int
    default_ocr_provider: str
    azure_key: str
    azure_endpoint: str

    @classmethod
    def load(cls) -> "ReportFactorSettings":
        upload_raw = os.getenv("ALPHAPILOT_REPORT_FACTOR_UPLOAD_DIR")
        upload_root = (
            Path(upload_raw).expanduser().resolve()
            if upload_raw
            else (important_data_dir() / "report_factor" / "uploads").resolve()
        )
        max_upload_mb = _env_int("ALPHAPILOT_REPORT_FACTOR_MAX_UPLOAD_MB", 50)
        return cls(
            upload_root=upload_root,
            max_upload_bytes=max_upload_mb * 1024 * 1024,
            min_text_chars=_env_int("ALPHAPILOT_REPORT_FACTOR_MIN_TEXT_CHARS", 500),
            chunk_token_limit=_env_int("ALPHAPILOT_REPORT_FACTOR_CHUNK_TOKENS", 12_000),
            max_evidence_chars=_env_int("ALPHAPILOT_REPORT_FACTOR_MAX_EVIDENCE_CHARS", 500),
            max_evidence_items=_env_int("ALPHAPILOT_REPORT_FACTOR_MAX_EVIDENCE_ITEMS", 3),
            llm_json_retries=_env_int(
                "ALPHAPILOT_REPORT_FACTOR_LLM_JSON_RETRIES", 2, minimum=0
            ),
            default_ocr_provider=(
                (os.getenv("ALPHAPILOT_REPORT_FACTOR_OCR_PROVIDER") or "azure")
                .strip()
                .lower()
                or "azure"
            ),
            azure_key=(
                os.getenv("ALPHAPILOT_REPORT_FACTOR_AZURE_KEY")
                or os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
                or ""
            ).strip(),
            azure_endpoint=(
                os.getenv("ALPHAPILOT_REPORT_FACTOR_AZURE_ENDPOINT")
                or os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
                or ""
            ).strip(),
        )
