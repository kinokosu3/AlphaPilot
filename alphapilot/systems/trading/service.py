"""Kernel service for strategy definitions, instances and promotion gates."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphapilot.kernel.base import BaseSystem
from alphapilot.systems.timing.base import TimingBacktestRequest
from alphapilot.systems.trading.domain import LifecycleState, StrategyInstanceConfig
from alphapilot.systems.trading.authorization import AutomatedRouteAuthorizer
from alphapilot.systems.trading.deployment import DeploymentCoordinator
from alphapilot.systems.trading.registry import (
    StrategyRegistry,
    resolve_required_history,
    validate_parameters,
)
from alphapilot.systems.trading.store import StrategyRuntimeStore

if TYPE_CHECKING:
    from alphapilot.kernel.context import Context


class TradingStrategySystem(BaseSystem):
    name = "trading"

    def setup(self, context: "Context") -> None:
        self.context = context
        local_root = os.getenv("ALPHAPILOT_STRATEGY_DIR") or str(Path.cwd() / "strategies")
        self.registry = StrategyRegistry(local_root=local_root).discover()
        live = context.engine.get_system("live")
        self.store = StrategyRuntimeStore(Path(live.config.state_dir) / "strategy_runtime.sqlite3")
        self._rebind_definition_hashes()
        self.route_authorizer = AutomatedRouteAuthorizer(self.store)
        self.deployment_coordinator = DeploymentCoordinator(
            self.store,
            live.runtime_control(),
        )

    def list_definitions(self) -> dict[str, Any]:
        return {
            "definitions": [item.to_dict() for item in self.registry.list()],
            "quarantined": self.registry.quarantined(),
            "entry_point_group": "alphapilot.strategies",
        }

    def list_instances(self) -> list[dict[str, Any]]:
        return self.store.list_instances()

    def create_instance(self, payload: dict[str, Any]) -> dict[str, Any]:
        definition = self.registry.get(str(payload.get("strategy_id") or ""))
        config = StrategyInstanceConfig(
            instance_id=str(payload.get("instance_id") or ""),
            strategy_id=definition.strategy_id,
            strategy_version=str(payload.get("strategy_version") or definition.version),
            params=dict(payload.get("params") or {}),
            universe=tuple(payload.get("universe") or ()),
            frequency=str(payload.get("frequency") or "day"),
            data_policy=dict(payload.get("data_policy") or {}),
            portfolio_policy=dict(payload.get("portfolio_policy") or {}),
            strategy_code_hash=definition.code_hash,
        )
        if config.strategy_version != definition.version:
            raise ValueError("strategy_version must match the registered definition")
        return self.store.create_instance(config)

    def update_instance(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.store.get_instance(instance_id)
        definition = self.registry.get(current["strategy_id"])
        safe_payload = {
            key: value for key, value in payload.items()
            if key in {"params", "universe", "frequency", "data_policy", "portfolio_policy"}
        }
        safe_payload["strategy_code_hash"] = definition.code_hash
        return self.store.update_instance(instance_id, safe_payload)

    def validate_instance(self, instance_id: str) -> dict[str, Any]:
        row = self.store.get_instance(instance_id)
        config = StrategyInstanceConfig.from_dict(row["config"])
        definition = self.registry.get(config.strategy_id)
        errors = validate_parameters(definition.parameter_schema, config.params)
        if config.strategy_version != definition.version:
            errors.append("strategy version changed; update the instance before validation")
        if not definition.code_hash:
            errors.append("strategy definition has no trusted code hash")
        elif config.strategy_code_hash != definition.code_hash:
            errors.append("strategy code hash changed; update the instance before validation")
        if config.frequency not in definition.supported_frequencies:
            errors.append(f"frequency {config.frequency!r} is not supported")
        if not config.universe:
            errors.append("universe must not be empty")
        if errors:
            return {"ok": False, "errors": errors, "instance": row}
        if row["lifecycle"] in {
            LifecycleState.CREATED.value,
            LifecycleState.VALIDATED.value,
        }:
            updated_runtime = self.store.get_runtime_state(instance_id)
            self.store.transition_runtime(
                instance_id,
                lifecycle=LifecycleState.VALIDATED.value,
                desired_state=LifecycleState.VALIDATED.value,
                observed_state=LifecycleState.VALIDATED.value,
                reconcile_required=(
                    updated_runtime["reconcile_required"]
                    if updated_runtime["deployment_level"] == "live" else False
                ),
            )
            updated = self.store.get_instance(instance_id)
        else:
            updated = row
        live = self.context.engine.get_system("live")
        target_pct = float(config.params.get("target_percent", 1.0) or 0.0)
        deployment_errors = []
        if target_pct > float(live.config.risk.max_position_pct) + 1e-9:
            deployment_errors.append(
                f"target_percent {target_pct:.1%} exceeds automated max_position_pct "
                f"{live.config.risk.max_position_pct:.1%}"
            )
        return {
            "ok": True,
            "errors": [],
            "deployment_ready": not deployment_errors,
            "deployment_errors": deployment_errors,
            "instance": updated,
            "required_history": resolve_required_history(definition, config.params),
        }

    def backtest_instance(self, instance_id: str, payload: dict[str, Any]) -> Any:
        validation = self.validate_instance(instance_id)
        if not validation["ok"]:
            raise ValueError("; ".join(validation["errors"]))
        config = StrategyInstanceConfig.from_dict(validation["instance"]["config"])
        request = TimingBacktestRequest(
            strategy_name=config.strategy_id,
            symbols=list(config.universe),
            start_date=payload.get("start_date"),
            end_date=payload.get("end_date"),
            freq=config.frequency,
            data_dir=payload.get("data_dir"),
            adjust_mode=str(payload.get("adjust_mode") or "backward"),
            execution_adjust_mode=str(payload.get("execution_adjust_mode") or "none"),
            cash=float(payload.get("cash") or 100000.0),
            target_percent=float(config.params.get("target_percent", payload.get("target_percent", 1.0))),
            strategy_params=dict(config.params),
            output_dir=payload.get("output_dir"),
        )
        result = self.context.engine.get_system("timing").run_backtest(request)
        self.store.record_stage(
            instance_id,
            "replay",
            passed=True,
            details=result.summary,
            expected_config_hash=config.config_hash,
        )
        return result

    def deployment(self, instance_id: str) -> dict[str, Any]:
        return self.store.deployment(instance_id)

    def promote(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        validation = self.validate_instance(instance_id)
        if not validation["ok"]:
            raise ValueError("; ".join(validation["errors"]))
        config = StrategyInstanceConfig.from_dict(validation["instance"]["config"])
        live = self.context.engine.get_system("live")
        target_pct = float(config.params.get("target_percent", 1.0) or 0.0)
        if target_pct > float(live.config.risk.max_position_pct) + 1e-9:
            raise ValueError(
                f"target_percent {target_pct:.1%} exceeds automated max_position_pct "
                f"{live.config.risk.max_position_pct:.1%}"
            )
        current = self.store.get_instance(instance_id)
        minimums = {"paper": 20, "shadow": 5}
        if current["deployment_level"] in minimums:
            evidence = self.store.evaluate_stage(
                instance_id,
                current["deployment_level"],
                minimum_sessions=minimums[current["deployment_level"]],
            )
            if not evidence["passed"]:
                raise ValueError(
                    f"{current['deployment_level']} evidence is insufficient: "
                    f"{evidence['trading_sessions']}/{evidence['minimum_sessions']} trading sessions"
                )
        return self.store.promote(
            instance_id,
            str(payload.get("to") or ""),
            account_id=str(payload.get("account_id") or ""),
            broker=str(payload.get("broker") or ""),
            approval=str(payload.get("approval") or ""),
        )

    def lifecycle_action(self, instance_id: str, action: str) -> dict[str, Any]:
        handlers = {
            "status": self.deployment_coordinator.status,
            "start": self.deployment_coordinator.start,
            "pause": self.deployment_coordinator.pause,
            "reconcile": self.deployment_coordinator.reconcile,
            "resume": self.deployment_coordinator.resume,
            "stop": self.deployment_coordinator.stop,
        }
        if action not in handlers:
            raise ValueError(f"unsupported lifecycle action {action!r}")
        return handlers[action](instance_id)

    def set_kill_switch(
        self,
        scope_type: str,
        scope_id: str,
        *,
        active: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        return self.store.set_route_block(
            scope_type,
            scope_id,
            active=active,
            reason=reason,
        )

    def list_kill_switches(self) -> list[dict[str, Any]]:
        return self.store.list_route_blocks()

    def start_stage_run(self, instance_id: str, stage: str) -> dict[str, Any]:
        if stage not in {"paper", "shadow"}:
            raise ValueError("stage run evidence is supported only for PAPER and SHADOW")
        return self.store.start_stage_run(instance_id, stage)

    def finish_stage_run(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.finish_stage_run(
            run_id,
            trading_sessions=int(payload.get("trading_sessions") or 0),
            metrics=dict(payload.get("metrics") or {}),
            status=str(payload.get("status") or "completed"),
        )

    def evaluate_stage(self, instance_id: str, stage: str) -> dict[str, Any]:
        minimums = {"paper": 20, "shadow": 5}
        if stage not in minimums:
            raise ValueError("only PAPER and SHADOW have mechanical stage-run gates")
        return self.store.evaluate_stage(
            instance_id,
            stage,
            minimum_sessions=minimums[stage],
        )

    def _rebind_definition_hashes(self) -> None:
        """Invalidate old evidence when installed strategy code has changed."""

        for row in self.store.list_instances():
            try:
                definition = self.registry.get(row["strategy_id"])
                config = StrategyInstanceConfig.from_dict(row["config"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                config.strategy_version != definition.version
                or (definition.code_hash and config.strategy_code_hash != definition.code_hash)
            ):
                self.store.update_instance(
                    row["instance_id"],
                    {
                        "strategy_version": definition.version,
                        "strategy_code_hash": definition.code_hash,
                    },
                )
