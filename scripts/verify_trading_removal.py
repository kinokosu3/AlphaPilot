#!/usr/bin/env python3
"""Run the fixed compatibility-removal verification suite and emit an attestation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any

from alphapilot.systems.trading.release_verification import (
    REMOVAL_BASE_COMMIT,
    REMOVAL_RELEASE,
    REPORT_SCHEMA_VERSION,
    canonical_report_hash,
    report_path_for,
    required_checks_for,
)


ROOT = Path(__file__).resolve().parents[1]
PORTAL = ROOT / "alphapilot/modules/portal/web"
PYTHON_COVERAGE = ROOT / "reports/trading-python-coverage.json"
TS_COVERAGE = ROOT / "git_ignore_folder/qa/portal_interaction/coverage/coverage-final.json"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _run(name: str, command: list[str], *, cwd: Path = ROOT) -> dict[str, Any]:
    started = time.monotonic()
    process = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    output = (process.stdout or "") + (process.stderr or "")
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    return {
        "name": name,
        "passed": process.returncode == 0,
        "returncode": process.returncode,
        "command": shlex.join(command),
        "working_directory": str(cwd.relative_to(ROOT) or Path(".")),
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def _wheel_smoke() -> dict[str, Any]:
    started = time.monotonic()
    outputs: list[str] = []
    returncode = 0
    with tempfile.TemporaryDirectory(prefix="alphapilot-wheel-smoke-") as raw:
        root = Path(raw)
        wheel_dir = root / "wheel"
        target = root / "site"
        commands = (
            [
                sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
                "--no-build-isolation", "--wheel-dir", str(wheel_dir),
            ],
            None,
        )
        build = subprocess.run(commands[0], cwd=ROOT, text=True, capture_output=True)
        outputs.extend([build.stdout or "", build.stderr or ""])
        returncode = build.returncode
        wheels = sorted(wheel_dir.glob("alphapilot-*.whl"))
        if returncode == 0 and len(wheels) == 1:
            install = subprocess.run(
                [
                    sys.executable, "-m", "pip", "install", "--no-deps",
                    "--target", str(target), str(wheels[0]),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            outputs.extend([install.stdout or "", install.stderr or ""])
            returncode = install.returncode
        elif returncode == 0:
            outputs.append(f"expected one wheel, found {len(wheels)}\n")
            returncode = 1
        if returncode == 0:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(target)
            smoke = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import alphapilot; import alphapilot.systems.trading.service; "
                    "import alphapilot.modules.portal.api",
                ],
                cwd=root,
                env=env,
                text=True,
                capture_output=True,
            )
            outputs.extend([smoke.stdout or "", smoke.stderr or ""])
            returncode = smoke.returncode
    output = "".join(outputs)
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    return {
        "name": "wheel_smoke",
        "passed": returncode == 0,
        "returncode": returncode,
        "command": "pip wheel + isolated target install/import smoke",
        "working_directory": ".",
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-kind",
        choices=("compatibility", "removal"),
        default="compatibility",
        help="emit the pre-removal compatibility report or post-removal report",
    )
    args = parser.parse_args()
    build_kind = str(args.build_kind)
    commit = _git("rev-parse", "HEAD")
    if _git("status", "--porcelain"):
        raise SystemExit(
            f"refusing to attest a dirty worktree; commit the {build_kind} build first"
        )
    if _git("merge-base", REMOVAL_BASE_COMMIT, commit) != REMOVAL_BASE_COMMIT:
        raise SystemExit("current commit is not descended from the fixed compatibility baseline")

    (ROOT / "reports").mkdir(parents=True, exist_ok=True)
    checks: dict[str, dict[str, Any]] = {}
    checks["backend_pytest"] = _run(
        "backend_pytest",
        [
            sys.executable, "-m", "coverage", "run", "--branch",
            "--source=alphapilot", "-m", "pytest", "-q",
        ],
    )
    if checks["backend_pytest"]["passed"]:
        export = _run(
            "python_coverage_export",
            [
                sys.executable, "-m", "coverage", "json", "-o",
                str(PYTHON_COVERAGE),
            ],
        )
        checks["backend_pytest"]["coverage_export"] = export
        checks["backend_pytest"]["passed"] = bool(export["passed"])
    checks["portal_test_coverage"] = _run(
        "portal_test_coverage",
        [
            "npm", "run", "test:coverage", "--",
            "--coverage.reporter=text", "--coverage.reporter=json",
        ],
        cwd=PORTAL,
    )
    checks["portal_typecheck"] = _run(
        "portal_typecheck", ["npm", "run", "typecheck"], cwd=PORTAL,
    )
    checks["portal_build"] = _run(
        "portal_build", ["npm", "run", "build"], cwd=PORTAL,
    )
    checks["openapi_contract"] = _run(
        "openapi_contract",
        [sys.executable, "-m", "pytest", "-q", "tests/test_portal_openapi_contract.py"],
    )
    checks["cli_contract"] = _run(
        "cli_contract",
        [sys.executable, "-m", "pytest", "-q", "tests/test_cli_contract_matrix.py"],
    )
    checks["dependency_boundary"] = _run(
        "dependency_boundary",
        [
            sys.executable, "-m", "pytest", "-q", "tests/test_trading_full_chain.py",
            "-k", "core_dependency_boundary",
        ],
    )
    if PYTHON_COVERAGE.is_file() and TS_COVERAGE.is_file():
        checks["changed_line_coverage"] = _run(
            "changed_line_coverage",
            [
                sys.executable, "scripts/check_changed_line_coverage.py",
                "--python-report", str(PYTHON_COVERAGE),
                "--typescript-report", str(TS_COVERAGE),
            ],
        )
    else:
        checks["changed_line_coverage"] = {
            "name": "changed_line_coverage",
            "passed": False,
            "returncode": 1,
            "command": "coverage reports required",
            "working_directory": ".",
            "duration_seconds": 0.0,
            "output_sha256": hashlib.sha256(b"coverage reports missing").hexdigest(),
        }
    checks["wheel_smoke"] = _wheel_smoke()
    checks["formal_interface_matrix"] = _run(
        "formal_interface_matrix",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_portal_trading.py",
            "tests/test_trading_cli_migration.py",
            "tests/test_live_runtime.py",
            "tests/test_trading_full_chain.py",
            "tests/test_trading_strategy_runtime.py",
        ],
    )
    checks["real_cli_smoke"] = _run(
        "real_cli_smoke",
        [sys.executable, "-m", "pytest", "-q", "tests/test_trading_cli_process_smoke.py"],
    )
    checks["secret_leak_scan"] = _run(
        "secret_leak_scan",
        [
            sys.executable,
            "scripts/check_secret_leaks.py",
            "--secret-file",
            ".env",
            "--secret-file",
            "secrets.txt",
            "--scan",
            "git_ignore_folder/acceptance/0.2.0",
            "--scan",
            "reports",
        ],
    )
    if build_kind == "compatibility":
        checks["compatibility_equivalence"] = _run(
            "compatibility_equivalence",
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/test_trading_compatibility_uat.py",
                "tests/test_broker_uat_local_security.py",
            ],
        )
    else:
        checks["legacy_absence"] = _run(
            "legacy_absence",
            [sys.executable, "scripts/check_legacy_entrypoint_absence.py"],
        )

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "removal_release": REMOVAL_RELEASE,
        "build_kind": build_kind,
        "base_commit": REMOVAL_BASE_COMMIT,
        "commit": commit,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": checks,
    }
    report["report_hash"] = canonical_report_hash(report)
    destination = ROOT / report_path_for(build_kind)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    required = required_checks_for(build_kind)
    passed = all(bool(checks.get(name, {}).get("passed")) for name in required)
    print(f"release verification report: {destination}")
    print(f"result: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
