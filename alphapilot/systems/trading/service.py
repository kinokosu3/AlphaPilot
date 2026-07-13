"""Kernel service for strategy definitions, instances and promotion gates."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from alphapilot.kernel.base import BaseSystem
from alphapilot.systems.timing.base import TimingBacktestRequest
from alphapilot.systems.trading.domain import LifecycleState, StrategyInstanceConfig
from alphapilot.systems.trading.registry import StrategyRegistry, validate_parameters
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
        )
        if config.strategy_version != definition.version:
            raise ValueError("strategy_version must match the registered definition")
        return self.store.create_instance(config)

    def update_instance(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.update_instance(instance_id, payload)

    def validate_instance(self, instance_id: str) -> dict[str, Any]:
        row = self.store.get_instance(instance_id)
        config = StrategyInstanceConfig.from_dict(row["config"])
        definition = self.registry.get(config.strategy_id)
        errors = validate_parameters(definition.parameter_schema, config.params)
        if config.frequency not in definition.supported_frequencies:
            errors.append(f"frequency {config.frequency!r} is not supported")
        if not config.universe:
            errors.append("universe must not be empty")
        if errors:
            return {"ok": False, "errors": errors, "instance": row}
        updated = self.store.set_lifecycle(instance_id, LifecycleState.VALIDATED.value)
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
            "required_history": definition.required_history,
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
        self.store.record_stage(instance_id, "replay", passed=True, details=result.summary)
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
        return self.store.promote(
            instance_id,
            str(payload.get("to") or ""),
            account_id=str(payload.get("account_id") or ""),
            broker=str(payload.get("broker") or ""),
            approval=str(payload.get("approval") or ""),
        )

    def lifecycle_action(self, instance_id: str, action: str) -> dict[str, Any]:
        mapping = {
            "pause": LifecycleState.PAUSED.value,
            "resume": LifecycleState.RUNNING.value,
            "stop": LifecycleState.STOPPED.value,
        }
        if action not in mapping:
            raise ValueError(f"unsupported lifecycle action {action!r}")
        return self.store.set_lifecycle(instance_id, mapping[action])
