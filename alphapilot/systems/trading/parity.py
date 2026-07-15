"""Decision parity and deployment qualification derived from persisted truth."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any


class DecisionParityService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def compare(
        self,
        instance_id: str,
        *,
        replay_run_id: str,
        shadow_stage_run_id: str,
    ) -> dict[str, Any]:
        run = self.store.create_parity_run(
            instance_id,
            replay_run_id=replay_run_id,
            shadow_stage_run_id=shadow_stage_run_id,
        )
        instance = self.store.get_instance(instance_id)
        daily = str(instance.get("config", {}).get("frequency") or "day") == "day"
        replay = self._group_observations(
            self.store.list_decision_observations(
                instance_id,
                mode="replay",
                run_id=replay_run_id,
            ),
            config_hash=run["config_hash"],
            daily=daily,
        )
        shadow = self._group_observations(
            self.store.list_decision_observations(
                instance_id,
                mode="shadow",
                run_id=shadow_stage_run_id,
            ),
            config_hash=run["config_hash"],
            daily=daily,
        )
        for session in sorted(set(replay) | set(shadow)):
            left_rows = replay.get(session, [])
            right_rows = shadow.get(session, [])
            if len(left_rows) > 1 or len(right_rows) > 1:
                status, reason, details = (
                    "not_comparable",
                    "multiple decision observations exist for one comparison session",
                    {"replay_count": len(left_rows), "shadow_count": len(right_rows)},
                )
                left = left_rows[0] if len(left_rows) == 1 else None
                right = right_rows[0] if len(right_rows) == 1 else None
            else:
                left = left_rows[0] if left_rows else None
                right = right_rows[0] if right_rows else None
                status, reason, details = self._compare_observations(left, right)
            self.store.record_parity_result(
                run["parity_run_id"],
                session,
                status=status,
                reason=reason,
                replay_observation_id="" if left is None else left["observation_id"],
                shadow_observation_id="" if right is None else right["observation_id"],
                details=details,
            )
        return self.store.finish_parity_run(
            run["parity_run_id"],
            details={
                "replay_run_id": replay_run_id,
                "shadow_stage_run_id": shadow_stage_run_id,
            },
        )

    @staticmethod
    def _group_observations(
        rows: list[dict[str, Any]],
        *,
        config_hash: str,
        daily: bool,
    ) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in rows:
            if item["config_hash"] != config_hash:
                continue
            as_of = str(item["as_of"])
            key = as_of[:10] if daily else as_of
            grouped.setdefault(key, []).append(item)
        return grouped

    @staticmethod
    def _compare_observations(
        replay: dict[str, Any] | None,
        shadow: dict[str, Any] | None,
    ) -> tuple[str, str, dict[str, Any]]:
        if replay is None or shadow is None:
            missing = "replay" if replay is None else "shadow"
            return "not_comparable", f"{missing} observation is missing", {"missing": missing}
        inputs = (
            "history_hash",
            "provider_state_before_hash",
            "data_version",
            "model_version",
            "policy_version",
        )
        differences = {
            key: {"replay": replay[key], "shadow": shadow[key]}
            for key in inputs
            if str(replay[key]) != str(shadow[key])
        }
        if differences:
            return "not_comparable", "decision inputs differ", {"differences": differences}
        outputs = {
            key: {"replay": replay[key], "shadow": shadow[key]}
            for key in ("signal_hash", "weights_hash")
            if str(replay[key]) != str(shadow[key])
        }
        if outputs:
            return "mismatch", "deterministic decision outputs differ", {"differences": outputs}
        execution_inputs = ("account_hash", "quote_hash", "instrument_hash")
        execution_comparable = all(
            replay[key] and replay[key] == shadow[key] for key in execution_inputs
        )
        execution_match = (
            execution_comparable
            and bool(replay["plan_hash"])
            and replay["plan_hash"] == shadow["plan_hash"]
        )
        return "pass", "signal and target weights match", {
            "execution_comparable": execution_comparable,
            "execution_match": execution_match,
        }


class DeploymentQualificationService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def evaluate(
        self,
        instance_id: str,
        *,
        account_id: str = "",
        broker: str = "",
        environment: str = "",
        plugin_version: str = "",
        plugin_hash: str = "",
        sdk_version: str = "",
        sdk_hash: str = "",
        runtime_code_hash: str = "",
    ) -> dict[str, Any]:
        current = self.store.get_instance(instance_id)
        runtime = self.store.get_runtime_state(instance_id)
        paper = self.store.evaluate_stage(instance_id, "paper", minimum_sessions=20)
        shadow = self.store.evaluate_stage(instance_id, "shadow", minimum_sessions=5)
        shadow_sessions = set(
            self.store.list_stage_sessions(
                instance_id,
                "shadow",
                config_hash=current["config_hash"],
            )
        )
        parity_runs = [
            row for row in self.store.list_parity_runs(instance_id)
            if row["config_hash"] == current["config_hash"]
            and row["status"] in {"passed", "failed"}
        ]
        statuses_by_session: dict[str, set[str]] = {}
        for run in parity_runs:
            for result in run["results"]:
                statuses_by_session.setdefault(str(result["session"]), set()).add(
                    str(result["status"])
                )
        parity_sessions = {
            session
            for session, statuses in statuses_by_session.items()
            if statuses == {"pass"}
        }
        invalid_sessions = {
            session: sorted(statuses)
            for session, statuses in statuses_by_session.items()
            if statuses != {"pass"}
        }
        parity = {
            "passed": (
                bool(shadow_sessions)
                and shadow_sessions <= parity_sessions
                and not (shadow_sessions & set(invalid_sessions))
            ),
            "required_sessions": sorted(shadow_sessions),
            "passed_sessions": sorted(parity_sessions & shadow_sessions),
            "missing_sessions": sorted(shadow_sessions - parity_sessions),
            "invalid_sessions": {
                key: invalid_sessions[key]
                for key in sorted(shadow_sessions & set(invalid_sessions))
            },
        }
        selected_account = str(account_id or runtime.get("account_id") or "")
        account_hash = _account_hash(selected_account)
        selected_broker = str(broker or runtime.get("broker") or "").lower()
        uat_evidence = (
            self.store.valid_broker_uat_evidence(
                selected_broker,
                account_hash=account_hash,
                environment=str(environment),
                plugin_version=str(plugin_version),
                plugin_hash=str(plugin_hash),
                sdk_version=str(sdk_version),
                sdk_hash=str(sdk_hash),
                runtime_code_hash=str(runtime_code_hash),
                scenario_version=2,
            )
            if selected_broker in {"xtp", "emt"} and account_hash else None
        )
        uat = {
            "required": selected_broker in {"xtp", "emt"},
            "passed": selected_broker not in {"xtp", "emt"} or uat_evidence is not None,
            "broker": selected_broker,
            "account_hash": account_hash,
            "environment": str(environment),
            "plugin_version": str(plugin_version),
            "plugin_hash": str(plugin_hash),
            "sdk_version": str(sdk_version),
            "sdk_hash": str(sdk_hash),
            "runtime_code_hash": str(runtime_code_hash),
            "evidence_id": "" if uat_evidence is None else uat_evidence["evidence_id"],
            "expires_at": "" if uat_evidence is None else uat_evidence["expires_at"],
        }
        reconcile = {
            "passed": not bool(runtime.get("reconcile_required")),
            "required": bool(runtime.get("reconcile_required")),
        }
        configuration = {
            "passed": current["deployment_level"] == "shadow",
            "deployment_level": current["deployment_level"],
            "config_hash": current["config_hash"],
        }
        eligible = all((
            paper["passed"], shadow["passed"], parity["passed"], uat["passed"],
            reconcile["passed"], configuration["passed"],
        ))
        result = {
            "instance_id": instance_id,
            "config_hash": current["config_hash"],
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "paper": paper,
            "shadow": shadow,
            "parity": parity,
            "broker_uat": uat,
            "reconcile": reconcile,
            "configuration": configuration,
            "eligible_for_live_authorization": eligible,
        }
        save_projection = getattr(self.store, "save_qualification_projection", None)
        if callable(save_projection):
            save_projection(result)
        return result


def _account_hash(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest() if account_id else ""
