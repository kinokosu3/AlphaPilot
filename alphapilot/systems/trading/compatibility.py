"""Fixed compatibility manifest and non-bypassable removal readiness checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any

from alphapilot.systems.trading.release_verification import (
    validate_release_verification,
)


DEPRECATED_SINCE = "0.1.x"
REMOVAL_RELEASE = "0.2.0"
# This is release metadata, deliberately not configurable through environment
# variables at runtime.  Change it only as part of a reviewed release commit.
SUNSET_AT = "Thu, 31 Dec 2026 00:00:00 GMT"
ENVIRONMENT_REPORT_SCHEMA = 1


def compatibility_environment_report_hash(payload: dict[str, Any]) -> str:
    material = dict(payload)
    material.pop("evidence_hash", None)
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def validate_compatibility_environment_report(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("compatibility environment report must be a JSON object")
    if int(payload.get("schema_version") or 0) != ENVIRONMENT_REPORT_SCHEMA:
        raise ValueError("unsupported compatibility environment report schema")
    if int(payload.get("runtime_schema_version") or 0) < 8:
        raise ValueError("compatibility environment report predates runtime schema v8")
    if not str(payload.get("environment_id") or "").strip():
        raise ValueError("compatibility environment report has no environment_id")
    cutoff = str(payload.get("migration_cutoff") or "").strip()
    if not cutoff:
        raise ValueError("compatibility environment report has no migration cutoff")
    generated_at = str(payload.get("generated_at") or "").strip()
    if not generated_at:
        raise ValueError("compatibility environment report has no generated_at")
    try:
        cutoff_time = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        generated_time = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("compatibility environment report timestamps must be ISO-8601") from exc
    if cutoff_time.tzinfo is None:
        cutoff_time = cutoff_time.replace(tzinfo=timezone.utc)
    if generated_time.tzinfo is None:
        generated_time = generated_time.replace(tzinfo=timezone.utc)
    if generated_time.astimezone(timezone.utc) < cutoff_time.astimezone(timezone.utc):
        raise ValueError("compatibility environment report predates its migration cutoff")
    commit = str(payload.get("code_commit") or "").strip().lower()
    if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
        raise ValueError("compatibility environment report has no full Git commit binding")
    expected = compatibility_environment_report_hash(payload)
    if str(payload.get("evidence_hash") or "") != expected:
        raise ValueError("compatibility environment report hash is invalid")
    entrypoints = payload.get("entrypoints")
    if not isinstance(entrypoints, list):
        raise ValueError("compatibility environment report entrypoints are missing")
    reported_total = sum(max(int(item.get("post_cutoff_count") or 0), 0) for item in entrypoints)
    if reported_total != max(int(payload.get("post_cutoff_count") or 0), 0):
        raise ValueError("compatibility environment report totals are inconsistent")
    for field in ("active_legacy_runtime_count", "unmigrated_legacy_job_count"):
        if field not in payload or int(payload[field]) < 0:
            raise ValueError(f"compatibility environment report has invalid {field}")
    return dict(payload)


@dataclass(frozen=True)
class CompatibilityEntrypoint:
    entrypoint: str
    kind: str
    replacement: str


ENTRYPOINTS = (
    CompatibilityEntrypoint(
        "GET /api/timing/strategies", "api", "/api/trading/strategy-definitions",
    ),
    CompatibilityEntrypoint(
        "POST /api/timing/signal", "api", "/api/trading/strategy-instances/{id}/preview",
    ),
    CompatibilityEntrypoint(
        "POST /api/timing/backtest", "api", "/api/trading/strategy-instances/{id}/backtest-runs",
    ),
    CompatibilityEntrypoint(
        "GET /api/timing/jobs/{id}/detail", "api", "/api/trading/backtest-runs/{id}/detail",
    ),
    CompatibilityEntrypoint("CLI timing_strategies", "cli", "trading_definitions"),
    CompatibilityEntrypoint("CLI timing_signal", "cli", "trading_preview"),
    CompatibilityEntrypoint("CLI timing_backtest", "cli", "trading_backtest"),
    CompatibilityEntrypoint(
        "POST /api/live/daemon/strategy/status", "api", "/api/trading/deployments/{id}/status",
    ),
    CompatibilityEntrypoint(
        "POST /api/live/daemon/strategy/start", "api", "/api/trading/deployments/{id}/start",
    ),
    CompatibilityEntrypoint(
        "POST /api/live/daemon/strategy/pause", "api", "/api/trading/deployments/{id}/pause",
    ),
    CompatibilityEntrypoint(
        "POST /api/live/daemon/strategy/resume", "api", "/api/trading/deployments/{id}/resume",
    ),
    CompatibilityEntrypoint(
        "POST /api/live/daemon/strategy/stop", "api", "/api/trading/deployments/{id}/stop",
    ),
    CompatibilityEntrypoint(
        "daemon --timing-strategy", "daemon", "daemon --strategy-instance-id",
    ),
    CompatibilityEntrypoint(
        "POST /api/trading/strategy-instances/{id}/backtest",
        "api",
        "/api/trading/strategy-instances/{id}/backtest-runs",
    ),
    CompatibilityEntrypoint(
        "POST /api/trading/deployments/{id}/{action}",
        "api",
        "/api/trading/deployments/{id}/{explicit-action}",
    ),
    CompatibilityEntrypoint(
        "POST /api/trading/stage-runs/*", "api", "/api/trading/deployments/{id}/stage-runs",
    ),
)


def register_manifest(store: Any) -> None:
    for item in ENTRYPOINTS:
        store.register_compatibility_entrypoint(
            item.entrypoint,
            kind=item.kind,
            replacement=item.replacement,
            deprecated_since=DEPRECATED_SINCE,
            sunset_at=SUNSET_AT,
            removal_release=REMOVAL_RELEASE,
        )


class RemovalReadinessService:
    def __init__(self, store: Any, *, repository_root: str | Path) -> None:
        self.store = store
        self.repository_root = Path(repository_root)

    def evaluate(self, acceptance_instance_id: str) -> dict[str, Any]:
        compatibility = self.store.compatibility_status()
        cutoff_set = bool(compatibility) and all(row["migration_cutoff"] for row in compatibility)
        post_cutoff = sum(int(row["post_cutoff_count"] or 0) for row in compatibility)
        environments = self.store.compatibility_environment_status()
        environment_cutoff_set = bool(environments) and all(
            row["migration_cutoff"] for row in environments
        )
        environment_post_cutoff = sum(
            int(row["post_cutoff_count"] or 0) for row in environments
        )
        complete_environment_reports = bool(environments) and all(
            str(row.get("evidence_hash") or "") for row in environments
        )
        from alphapilot.systems.trading.parity import DeploymentQualificationService

        try:
            qualification = DeploymentQualificationService(self.store).evaluate(
                acceptance_instance_id
            )
        except (KeyError, ValueError) as exc:
            qualification = {
                "eligible_for_live_authorization": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        broker_evidence = {
            broker: self.store.valid_broker_uat_evidence(broker, scenario_version=2)
            for broker in ("xtp", "emt")
        }
        production_references = self._production_legacy_references()
        runtime_blockers = self.store.legacy_runtime_blockers()
        unmigrated_jobs = self._unmigrated_legacy_jobs()
        checks = {
            "schema_v8": self.store.schema_version >= 8,
            "migration_cutoff_set": cutoff_set and environment_cutoff_set,
            "zero_post_cutoff_calls": (
                cutoff_set and environment_cutoff_set
                and post_cutoff == 0 and environment_post_cutoff == 0
            ),
            "environment_reports_complete": complete_environment_reports,
            "xtp_uat": broker_evidence["xtp"] is not None,
            "emt_uat": broker_evidence["emt"] is not None,
            "first_party_zero_references": not production_references,
            "no_active_legacy_runtime": not any(runtime_blockers.values()),
            "all_legacy_jobs_imported": not unmigrated_jobs,
        }
        commit = self._git_state()
        checks["release_commit_clean"] = bool(commit["commit"]) and commit["clean"]
        checks["environment_report_commits_match"] = bool(commit["commit"]) and all(
            str((row.get("evidence") or {}).get("code_commit") or "") == commit["commit"]
            for row in environments
        )
        checks["all_environments_no_legacy_runtime"] = bool(environments) and all(
            int((row.get("evidence") or {}).get("active_legacy_runtime_count") or 0) == 0
            for row in environments
        )
        checks["all_environments_legacy_jobs_imported"] = bool(environments) and all(
            int((row.get("evidence") or {}).get("unmigrated_legacy_job_count") or 0) == 0
            for row in environments
        )
        release_verification = validate_release_verification(
            self.repository_root,
            expected_commit=str(commit.get("commit") or ""),
        )
        checks["release_verification"] = bool(release_verification["passed"])
        report = {
            "ready": all(checks.values()),
            "removal_release": REMOVAL_RELEASE,
            "sunset_at": SUNSET_AT,
            "acceptance_instance_id": acceptance_instance_id,
            "checks": checks,
            "post_cutoff_calls": post_cutoff,
            "environment_post_cutoff_calls": environment_post_cutoff,
            "compatibility": compatibility,
            "environments": environments,
            "live_qualification": qualification,
            "broker_uat": {
                key: None if value is None else {
                    "evidence_id": value["evidence_id"],
                    "evidence_hash": value["evidence_hash"],
                    "expires_at": value["expires_at"],
                }
                for key, value in broker_evidence.items()
            },
            "production_legacy_references": production_references,
            "legacy_runtime_blockers": runtime_blockers,
            "unmigrated_legacy_jobs": unmigrated_jobs,
            "code": commit,
            "release_verification": release_verification,
            "schema_version": self.store.schema_version,
        }
        evidence_material = {
            "commit": commit["commit"],
            "schema_version": self.store.schema_version,
            "qualification_config_hash": qualification.get("config_hash", ""),
            "release_verification_hash": release_verification.get("report_hash", ""),
            "environment_reports": {
                str(row["environment_id"]): str(row.get("evidence_hash") or "")
                for row in environments
            },
            "broker_uat": {
                key: "" if value is None else value["evidence_hash"]
                for key, value in broker_evidence.items()
            },
        }
        report["evidence_hash"] = hashlib.sha256(
            json.dumps(evidence_material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        report["report_hash"] = hashlib.sha256(
            json.dumps(report, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return report

    def _git_state(self) -> dict[str, Any]:
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=self.repository_root,
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], cwd=self.repository_root,
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            return {"commit": commit, "clean": not bool(dirty)}
        except (OSError, subprocess.SubprocessError):
            return {"commit": "", "clean": False}

    def _production_legacy_references(self) -> list[dict[str, Any]]:
        """Find first-party callers; compatibility definitions themselves are allowed."""

        roots = (self.repository_root / "alphapilot", self.repository_root / "scripts")
        needles = (
            "/api/timing",
            "/api/live/daemon/strategy",
            "timing_backtest",
            "timing_signal",
            "timing_strategies",
            "timing_strategy",
            "TimingBacktestEngine",
        )
        # These files are the temporary 0.1.x compatibility boundary or define
        # the legacy implementation itself.  A reference anywhere else is a
        # first-party caller and blocks removal.
        allowed = {
            "alphapilot/modules/live/module.py",
            "alphapilot/modules/portal/api.py",
            "alphapilot/modules/portal/jobs.py",
            "alphapilot/modules/timing/module.py",
            "alphapilot/systems/live/daemon.py",
            "alphapilot/systems/timing/compatibility.py",
            "alphapilot/systems/timing/engine.py",
            "alphapilot/systems/trading/compatibility.py",
            # This release-gate script contains the forbidden strings as data
            # so it can prove their absence after the removal commit. It never
            # invokes a legacy entrypoint.
            "scripts/check_legacy_entrypoint_absence.py",
        }
        hits: list[dict[str, Any]] = []
        for root in roots:
            paths = [root] if root.is_file() else list(root.rglob("*")) if root.exists() else []
            for path in paths:
                if (
                    not path.is_file()
                    or "node_modules" in path.parts
                    or path.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx"}
                ):
                    continue
                relative = path.relative_to(self.repository_root).as_posix()
                if relative in allowed:
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for needle in needles:
                    if needle in content:
                        hits.append({
                            "path": relative,
                            "needle": needle,
                        })
        return hits

    def _unmigrated_legacy_jobs(self) -> list[dict[str, str]]:
        root = Path(
            os.getenv("ALPHAPILOT_PORTAL_JOB_ROOT")
            or self.repository_root / "git_ignore_folder" / "portal_jobs"
        ).expanduser()
        if not root.is_dir():
            return []
        imported = {
            str(item["legacy_job_id"]) for item in self.store.list_legacy_job_imports()
        }
        missing: list[dict[str, str]] = []
        for metadata_path in sorted(root.glob("*/job.json")):
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job_id = str(payload.get("job_id") or metadata_path.parent.name)
            if (
                payload.get("kind") == "timing_backtest"
                and payload.get("status") == "succeeded"
                and job_id not in imported
            ):
                missing.append({"job_id": job_id, "path": str(metadata_path.parent)})
        return missing
