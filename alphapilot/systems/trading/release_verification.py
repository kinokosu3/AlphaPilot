"""Machine-verifiable release evidence for removing compatibility entrypoints.

The report is intentionally tied to one Git commit and produced by the fixed
``scripts/verify_trading_removal.py`` workflow.  Runtime configuration cannot
turn a missing or stale report into a pass.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


REPORT_SCHEMA_VERSION = 2
REMOVAL_RELEASE = "0.2.0"
# The compatibility migration started at this reviewed repository commit.  The
# diff-coverage gate compares the removal build with this exact baseline.
REMOVAL_BASE_COMMIT = "c28e89199fa10878a79764432b695bd463acdd86"
COMPATIBILITY_REPORT_RELATIVE_PATH = Path(
    "reports/trading-compatibility-verification.json"
)
REMOVAL_REPORT_RELATIVE_PATH = Path("reports/trading-removal-verification.json")
# Backward-compatible names used by the runtime removal gate and 0.1.x tests.
REPORT_RELATIVE_PATH = COMPATIBILITY_REPORT_RELATIVE_PATH
BASE_REQUIRED_CHECKS = (
    "backend_pytest",
    "portal_test_coverage",
    "portal_typecheck",
    "portal_build",
    "openapi_contract",
    "cli_contract",
    "dependency_boundary",
    "changed_line_coverage",
    "wheel_smoke",
)
COMPATIBILITY_REQUIRED_CHECKS = BASE_REQUIRED_CHECKS + (
    "formal_interface_matrix",
    "compatibility_equivalence",
    "real_cli_smoke",
    "secret_leak_scan",
)
REMOVAL_REQUIRED_CHECKS = BASE_REQUIRED_CHECKS + (
    "formal_interface_matrix",
    "real_cli_smoke",
    "legacy_absence",
    "secret_leak_scan",
)
REQUIRED_CHECKS = COMPATIBILITY_REQUIRED_CHECKS


def report_path_for(build_kind: str) -> Path:
    selected = str(build_kind).strip().lower()
    if selected == "compatibility":
        return COMPATIBILITY_REPORT_RELATIVE_PATH
    if selected == "removal":
        return REMOVAL_REPORT_RELATIVE_PATH
    raise ValueError("build_kind must be compatibility or removal")


def required_checks_for(build_kind: str) -> tuple[str, ...]:
    selected = str(build_kind).strip().lower()
    if selected == "compatibility":
        return COMPATIBILITY_REQUIRED_CHECKS
    if selected == "removal":
        return REMOVAL_REQUIRED_CHECKS
    raise ValueError("build_kind must be compatibility or removal")


def canonical_report_hash(payload: dict[str, Any]) -> str:
    material = dict(payload)
    material.pop("report_hash", None)
    return hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def validate_release_verification(
    repository_root: str | Path,
    *,
    expected_commit: str,
    build_kind: str = "compatibility",
) -> dict[str, Any]:
    """Validate the fixed release report without executing any build commands."""

    root = Path(repository_root).resolve()
    selected = str(build_kind).strip().lower()
    path = root / report_path_for(selected)
    required_checks = required_checks_for(selected)
    result: dict[str, Any] = {
        "passed": False,
        "path": str(path),
        "expected_commit": str(expected_commit),
        "build_kind": selected,
        "required_checks": list(required_checks),
        "errors": [],
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        result["errors"].append("release verification report is missing")
        return result
    except (OSError, json.JSONDecodeError) as exc:
        result["errors"].append(f"release verification report is unreadable: {exc}")
        return result
    if not isinstance(payload, dict):
        result["errors"].append("release verification report must be a JSON object")
        return result

    result["commit"] = str(payload.get("commit") or "")
    result["generated_at"] = str(payload.get("generated_at") or "")
    result["report_hash"] = str(payload.get("report_hash") or "")
    if int(payload.get("schema_version") or 0) != REPORT_SCHEMA_VERSION:
        result["errors"].append("release verification schema version is unsupported")
    if str(payload.get("removal_release") or "") != REMOVAL_RELEASE:
        result["errors"].append("release verification targets a different release")
    if str(payload.get("build_kind") or "") != selected:
        result["errors"].append("release verification is for a different build kind")
    if str(payload.get("base_commit") or "") != REMOVAL_BASE_COMMIT:
        result["errors"].append("release verification uses a different migration baseline")
    if not expected_commit or result["commit"] != str(expected_commit):
        result["errors"].append("release verification is not bound to the current commit")
    if not result["generated_at"]:
        result["errors"].append("release verification has no generation timestamp")
    expected_hash = canonical_report_hash(payload)
    if not result["report_hash"] or result["report_hash"] != expected_hash:
        result["errors"].append("release verification report hash is invalid")

    checks = payload.get("checks")
    if not isinstance(checks, dict):
        result["errors"].append("release verification checks are missing")
        checks = {}
    missing = sorted(set(required_checks) - set(checks))
    failed = sorted(
        name for name in required_checks
        if name in checks and not bool((checks.get(name) or {}).get("passed"))
    )
    if missing:
        result["errors"].append(f"release verification checks are missing: {missing}")
    if failed:
        result["errors"].append(f"release verification checks failed: {failed}")
    result["checks"] = {
        name: {
            "passed": bool((checks.get(name) or {}).get("passed")),
            "returncode": (checks.get(name) or {}).get("returncode"),
            "output_sha256": str((checks.get(name) or {}).get("output_sha256") or ""),
        }
        for name in required_checks
    }
    result["passed"] = not result["errors"]
    return result
