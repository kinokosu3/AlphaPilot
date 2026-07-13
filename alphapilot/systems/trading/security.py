"""Trust checks for executable strategy and model artifacts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def verify_trusted_model(path: str | Path, *, extra_roots: list[str | Path] | None = None) -> str:
    candidate = Path(path).expanduser().resolve(strict=True)
    if not candidate.is_file():
        raise ValueError(f"model artifact is not a file: {candidate}")
    if candidate.suffix.lower() not in {".pkl", ".pickle", ".model", ".txt", ".json"}:
        raise ValueError(f"unsupported model artifact suffix: {candidate.suffix}")
    if candidate.stat().st_mode & 0o002:
        raise ValueError("world-writable model artifacts are not trusted")
    roots: list[Path] = [Path.cwd().resolve()]
    raw = os.getenv("ALPHAPILOT_TRUSTED_MODEL_DIRS", "")
    roots.extend(Path(item).expanduser().resolve() for item in raw.split(os.pathsep) if item.strip())
    roots.extend(Path(item).expanduser().resolve() for item in (extra_roots or []))
    if not any(_is_relative_to(candidate, root) for root in roots):
        raise ValueError(
            "model artifact is outside trusted roots; set ALPHAPILOT_TRUSTED_MODEL_DIRS explicitly"
        )
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
