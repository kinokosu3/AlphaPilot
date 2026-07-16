"""Safe persistent storage for Portal-uploaded research reports."""

from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from alphapilot.core.path_safety import ensure_child_path
from alphapilot.kernel.paths import important_data_dir
from alphapilot.modules.report_factor.settings import ReportFactorSettings


def _safe_filename(filename: str | None) -> str:
    raw_name = filename or "report.pdf"
    if Path(raw_name).name != raw_name or "/" in raw_name or "\\" in raw_name:
        raise ValueError("Uploaded PDF filename must not contain path components")
    name = raw_name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not cleaned:
        cleaned = "report.pdf"
    if not cleaned.lower().endswith(".pdf"):
        raise ValueError("Uploaded report must use a .pdf extension")
    return cleaned


def _checked_upload_root(settings: ReportFactorSettings) -> Path:
    allowed_root = important_data_dir().resolve()
    upload_root = ensure_child_path(allowed_root, settings.upload_root)
    if upload_root == allowed_root:
        raise ValueError("Report-factor upload root must be below important_data")
    return upload_root


async def save_uploaded_pdf(upload: Any, settings: ReportFactorSettings) -> dict[str, Any]:
    filename = _safe_filename(getattr(upload, "filename", None))
    upload_root = _checked_upload_root(settings)
    upload_root.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    upload_dir = ensure_child_path(upload_root, upload_root / upload_id)
    upload_dir.mkdir(parents=False, exist_ok=False)
    target = ensure_child_path(upload_dir, upload_dir / filename)
    digest = hashlib.sha256()
    size = 0
    header = b""
    try:
        with target.open("xb") as handle:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise ValueError(
                        f"PDF exceeds upload limit of {settings.max_upload_bytes // (1024 * 1024)} MiB"
                    )
                if len(header) < 1024:
                    header += chunk[: 1024 - len(header)]
                digest.update(chunk)
                handle.write(chunk)
        if size == 0:
            raise ValueError("Uploaded PDF is empty")
        if b"%PDF-" not in header:
            raise ValueError("Uploaded file does not contain a valid PDF header")
    except Exception:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise
    finally:
        close = getattr(upload, "close", None)
        if close is not None:
            maybe = close()
            if hasattr(maybe, "__await__"):
                await maybe

    return {
        "upload_id": upload_id,
        "source": str(target),
        "file_name": filename,
        "size": size,
        "sha256": digest.hexdigest(),
    }


def delete_uploaded_pdf(upload_id: str, settings: ReportFactorSettings) -> bool:
    if not re.fullmatch(r"[0-9a-f]{32}", str(upload_id)):
        raise ValueError("Invalid report upload id")
    upload_root = _checked_upload_root(settings)
    upload_dir = ensure_child_path(upload_root, upload_root / upload_id)
    if not upload_dir.is_dir():
        raise FileNotFoundError(f"Report upload not found: {upload_id}")
    shutil.rmtree(upload_dir)
    return not upload_dir.exists()
