#!/usr/bin/env python3
"""Fail when acceptance artifacts contain values sourced from local secrets.

Only counts and affected paths are printed; matching secret values are never
included in diagnostics.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import stat
from typing import Any


def _secret_values(path: Path) -> set[bytes]:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"secret file {path} must use mode 0600")
    values: set[bytes] = set()
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if any(marker in key.upper() for marker in ("ACCOUNT", "PASSWORD", "KEY", "TOKEN")):
                candidate = value.strip().strip("'\"")
                if len(candidate) >= 4:
                    values.add(candidate.encode("utf-8"))
            continue
        if "：" in line:
            _key, value = line.split("：", 1)
            candidate = value.strip()
            if len(candidate) >= 4:
                values.add(candidate.encode("utf-8"))
            continue
        # The supplied broker note stores several values on the line following
        # a Chinese label. Keep only value-shaped rows and ignore prose/headers.
        if len(line) >= 4 and not any("\u4e00" <= char <= "\u9fff" for char in line):
            values.add(line.encode("utf-8"))
    return values


def _structured_strings(payload: bytes) -> list[str]:
    text = payload.decode("utf-8", errors="replace")
    documents: list[Any] = []
    try:
        documents.append(json.loads(text))
    except json.JSONDecodeError:
        for raw in text.splitlines():
            try:
                documents.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    values: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            values.append(value)

    for document in documents:
        walk(document)
    return values


def _scan_file(payload: bytes, secrets: set[bytes]) -> int:
    """Count leaks without treating a short PIN as any matching date fragment."""

    structured = _structured_strings(payload)
    text = payload.decode("utf-8", errors="replace")
    matches = 0
    for secret in secrets:
        if not secret:
            continue
        if len(secret) >= 8 and not secret.isdigit():
            matches += payload.count(secret)
            continue
        value = secret.decode("utf-8", errors="ignore")
        matches += sum(item == value for item in structured)
        # Short values are too collision-prone for an unrestricted substring
        # scan. Still catch key/value logs such as ``account_id=1234``.
        pattern = re.compile(
            r"(?i)(?:account(?:_?id)?|password|passwd|secret|token|software_key|api_key)"
            r"[\"']?\s*[:=]\s*[\"']?"
            + re.escape(value)
            + r"(?:[\"'\s,}]|$)"
        )
        matches += len(pattern.findall(text))
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-file", action="append", default=[])
    parser.add_argument("--scan", action="append", default=[])
    args = parser.parse_args()
    secrets: set[bytes] = set()
    secret_paths = {Path(item).expanduser().resolve() for item in args.secret_file}
    for path in secret_paths:
        if path.is_file():
            secrets.update(_secret_values(path))
    affected: list[str] = []
    matches = 0
    for raw_root in args.scan:
        root = Path(raw_root).expanduser().resolve()
        paths = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
        for path in paths:
            if path.resolve() in secret_paths:
                continue
            try:
                payload = path.read_bytes()
            except OSError:
                continue
            count = _scan_file(payload, secrets)
            if count:
                matches += count
                affected.append(str(path))
    print(f"secret leak scan: matches={matches} affected_files={len(affected)}")
    for path in affected:
        print(path)
    return 1 if matches else 0


if __name__ == "__main__":
    raise SystemExit(main())
