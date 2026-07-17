#!/usr/bin/env python3
"""Enforce 90% coverage on executable Python/TypeScript lines changed since 0.1.x."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from alphapilot.systems.trading.release_verification import REMOVAL_BASE_COMMIT


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _changed_lines(
    root: Path,
    *,
    include_working_tree: bool = False,
) -> dict[str, set[int]]:
    comparison = REMOVAL_BASE_COMMIT if include_working_tree else f"{REMOVAL_BASE_COMMIT}...HEAD"
    completed = subprocess.run(
        [
            "git", "diff", "--unified=0", "--no-ext-diff",
            comparison, "--", "alphapilot",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    changed: dict[str, set[int]] = {}
    current = ""
    for line in completed.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            changed.setdefault(current, set())
            continue
        match = HUNK.match(line)
        if not match or not current:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or 1)
        changed[current].update(range(start, start + count))
    if include_working_tree:
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "--", "alphapilot"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        for relative in untracked.stdout.splitlines():
            path = root / relative
            if path.is_file() and relative.endswith((".py", ".ts", ".tsx")):
                changed[relative] = set(range(1, len(path.read_text(encoding="utf-8").splitlines()) + 1))
    return {
        path: lines for path, lines in changed.items()
        if path.endswith(".py") or path.endswith((".ts", ".tsx"))
    }


def _python_coverage(path: Path) -> dict[str, dict[str, set[int]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, set[int]]] = {}
    for filename, item in dict(payload.get("files") or {}).items():
        normalized = str(filename).replace("\\", "/")
        result[normalized] = {
            "executable": set(item.get("executed_lines") or ())
            | set(item.get("missing_lines") or ()),
            "covered": set(item.get("executed_lines") or ()),
        }
    return result


def _typescript_coverage(path: Path, root: Path) -> dict[str, dict[str, set[int]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, set[int]]] = {}
    for filename, item in dict(payload or {}).items():
        source = Path(filename)
        try:
            normalized = source.resolve().relative_to(root).as_posix()
        except ValueError:
            normalized = source.as_posix()
        per_line: dict[int, list[bool]] = {}
        statements = dict(item.get("statementMap") or {})
        counts = dict(item.get("s") or {})
        for statement_id, location in statements.items():
            line = int((location.get("start") or {}).get("line") or 0)
            if line > 0:
                per_line.setdefault(line, []).append(int(counts.get(statement_id) or 0) > 0)
        result[normalized] = {
            "executable": set(per_line),
            "covered": {line for line, values in per_line.items() if all(values)},
        }
    return result


def _score(
    changed: dict[str, set[int]],
    coverage: dict[str, dict[str, set[int]]],
    suffixes: tuple[str, ...],
) -> dict[str, Any]:
    relevant = {
        path: lines
        for path, lines in changed.items()
        if lines
        and path.endswith(suffixes)
        and not (
            path.endswith((".test.ts", ".test.tsx", ".d.ts", "/types.ts", "/main.tsx"))
        )
    }
    missing_files = sorted(path for path in relevant if path not in coverage)
    executable: set[tuple[str, int]] = set()
    covered: set[tuple[str, int]] = set()
    for path, lines in relevant.items():
        file_coverage = coverage.get(path, {"executable": set(), "covered": set()})
        for line in lines & file_coverage["executable"]:
            executable.add((path, line))
            if line in file_coverage["covered"]:
                covered.add((path, line))
    percent = 100.0 if not executable else len(covered) * 100.0 / len(executable)
    return {
        "passed": not missing_files and percent >= 90.0,
        "percent": round(percent, 2),
        "covered_lines": len(covered),
        "executable_changed_lines": len(executable),
        "missing_files": missing_files,
        "uncovered": [f"{path}:{line}" for path, line in sorted(executable - covered)],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-report", required=True)
    parser.add_argument("--typescript-report", required=True)
    parser.add_argument("--output", default="reports/trading-diff-coverage.json")
    parser.add_argument(
        "--include-working-tree",
        action="store_true",
        help="development-only: include tracked and untracked working-tree changes",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    changed = _changed_lines(root, include_working_tree=args.include_working_tree)
    python = _score(
        changed,
        _python_coverage(Path(args.python_report)),
        (".py",),
    )
    typescript = _score(
        changed,
        _typescript_coverage(Path(args.typescript_report), root),
        (".ts", ".tsx"),
    )
    result = {"base_commit": REMOVAL_BASE_COMMIT, "python": python, "typescript": typescript}
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if python["passed"] and typescript["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
