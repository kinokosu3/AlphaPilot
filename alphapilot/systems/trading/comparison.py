"""Optional, mode-neutral comparison of persisted strategy decisions."""

from __future__ import annotations

from typing import Any


class DecisionComparisonService:
    def __init__(self, store: Any) -> None:
        self.store = store

    def compare(
        self,
        instance_id: str,
        *,
        left_mode: str,
        left_run_id: str,
        right_mode: str,
        right_run_id: str,
    ) -> dict[str, Any]:
        allowed_modes = {"replay", "paper", "simulation", "shadow", "live"}
        left_mode = str(left_mode).strip().lower()
        right_mode = str(right_mode).strip().lower()
        left_run_id = str(left_run_id).strip()
        right_run_id = str(right_run_id).strip()
        invalid = {left_mode, right_mode} - allowed_modes
        if invalid:
            raise ValueError(f"unsupported decision-comparison modes: {sorted(invalid)}")
        if not left_run_id or not right_run_id:
            raise ValueError("left_run_id and right_run_id are required")
        comparison = self.store.create_decision_comparison(
            instance_id,
            left_mode=left_mode,
            left_run_id=left_run_id,
            right_mode=right_mode,
            right_run_id=right_run_id,
        )
        instance = self.store.get_instance(instance_id)
        daily = str(instance.get("config", {}).get("frequency") or "day") == "day"
        left_rows = self._group_observations(
            self.store.list_decision_observations(
                instance_id, mode=left_mode, run_id=left_run_id,
            ),
            config_hash=comparison["config_hash"],
            daily=daily,
        )
        right_rows = self._group_observations(
            self.store.list_decision_observations(
                instance_id, mode=right_mode, run_id=right_run_id,
            ),
            config_hash=comparison["config_hash"],
            daily=daily,
        )
        for session in sorted(set(left_rows) | set(right_rows)):
            left_session = left_rows.get(session, [])
            right_session = right_rows.get(session, [])
            if len(left_session) > 1 or len(right_session) > 1:
                status, reason, details = (
                    "not_comparable",
                    "multiple decision observations exist for one comparison session",
                    {"left_count": len(left_session), "right_count": len(right_session)},
                )
                left = left_session[0] if len(left_session) == 1 else None
                right = right_session[0] if len(right_session) == 1 else None
            else:
                left = left_session[0] if left_session else None
                right = right_session[0] if right_session else None
                status, reason, details = self._compare_observations(left, right)
            self.store.record_decision_comparison_result(
                comparison["comparison_id"],
                session,
                status=status,
                reason=reason,
                left_observation_id="" if left is None else left["observation_id"],
                right_observation_id="" if right is None else right["observation_id"],
                details=details,
            )
        return self.store.finish_decision_comparison(
            comparison["comparison_id"],
            details={
                "left": {"mode": left_mode, "run_id": left_run_id},
                "right": {"mode": right_mode, "run_id": right_run_id},
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
        left: dict[str, Any] | None,
        right: dict[str, Any] | None,
    ) -> tuple[str, str, dict[str, Any]]:
        if left is None or right is None:
            missing = "left" if left is None else "right"
            return "not_comparable", f"{missing} observation is missing", {"missing": missing}
        inputs = (
            "history_hash",
            "provider_state_before_hash",
            "data_version",
            "model_version",
            "policy_version",
        )
        differences = {
            key: {"left": left[key], "right": right[key]}
            for key in inputs
            if str(left[key]) != str(right[key])
        }
        if differences:
            return "not_comparable", "decision inputs differ", {"differences": differences}
        outputs = {
            key: {"left": left[key], "right": right[key]}
            for key in ("provider_state_after_hash", "signal_hash", "weights_hash")
            if str(left[key]) != str(right[key])
        }
        if outputs:
            return "mismatch", "deterministic decision outputs differ", {
                "differences": outputs,
            }
        execution_inputs = ("account_hash", "quote_hash", "instrument_hash")
        execution_comparable = all(
            left[key] and left[key] == right[key] for key in execution_inputs
        )
        execution_match = (
            execution_comparable
            and bool(left["plan_hash"])
            and left["plan_hash"] == right["plan_hash"]
        )
        return "match", "signal and target weights match", {
            "execution_comparable": execution_comparable,
            "execution_match": execution_match,
        }
