"""Kernel service for strategy definitions, instances and promotion gates."""

from __future__ import annotations

import os
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import uuid
from typing import TYPE_CHECKING, Any

import pandas as pd

from alphapilot.kernel.base import BaseSystem
from alphapilot.systems.trading.domain import LifecycleState, StrategyInstanceConfig
from alphapilot.systems.trading.artifacts import ResearchArtifactSnapshotter, verify_artifact_binding
from alphapilot.systems.trading.authorization import AutomatedRouteAuthorizer
from alphapilot.systems.trading.deployment import DeploymentCoordinator
from alphapilot.systems.trading.registry import (
    StrategyRegistry,
    resolve_required_history,
    schema_defaults,
    validate_parameters,
)
from alphapilot.systems.trading.policy_registry import PortfolioPolicyRegistry
from alphapilot.systems.trading.contracts import SignalKind
from alphapilot.systems.trading.application import DecisionPipeline
from alphapilot.systems.trading.contracts import (
    AccountSnapshot,
    InstrumentMetadata,
    PortfolioDecision,
    PriceAdjustment,
    canonical_instrument,
)
from alphapilot.systems.trading.data_adapters import (
    SequenceCalendar,
    completed_bars_from_frame,
    instrument_metadata_from_frame,
    tradable_quotes_from_frame,
)
from alphapilot.systems.trading.replay import ReplayConfig, ReplayRuntime
from alphapilot.systems.trading.store import StrategyRuntimeStore
from alphapilot.systems.trading.operators import OperatorAuthService

if TYPE_CHECKING:
    from alphapilot.kernel.context import Context


def _option(payload: dict[str, Any], key: str, default: Any) -> Any:
    value = payload.get(key)
    return default if value is None or value == "" else value


class TradingStrategySystem(BaseSystem):
    name = "trading"

    def setup(self, context: "Context") -> None:
        self.context = context
        local_root = os.getenv("ALPHAPILOT_STRATEGY_DIR") or str(Path.cwd() / "strategies")
        from alphapilot.systems.selection.definitions import strategy_definitions as selection_definitions
        from alphapilot.systems.timing.definitions import strategy_definitions as timing_definitions

        self.registry = StrategyRegistry(local_root=local_root).discover(
            builtin_contributions=[*timing_definitions(), *selection_definitions()],
        )
        policy_root = os.getenv("ALPHAPILOT_PORTFOLIO_POLICY_DIR") or str(Path.cwd() / "policies")
        self.policy_registry = PortfolioPolicyRegistry(local_root=policy_root).discover()
        live = context.engine.get_system("live")
        self.store = StrategyRuntimeStore(Path(live.config.state_dir) / "strategy_runtime.sqlite3")
        self.artifact_snapshotter = ResearchArtifactSnapshotter(
            Path(live.config.state_dir) / "strategy_artifacts",
            strategy_system=context.engine.get_system("strategy"),
            config=context.config,
        )
        self.operator_auth = OperatorAuthService(self.store)
        self._backtest_executor = ThreadPoolExecutor(
            max_workers=max(int(os.getenv("ALPHAPILOT_TRADING_BACKTEST_WORKERS", "2")), 1),
            thread_name_prefix="alphapilot-trading-replay",
        )
        self._backtest_output_root = (
            Path(context.config.backtest.workspace_root).expanduser() / "trading-runs"
        )
        self._rebind_definition_hashes()
        self.route_authorizer = AutomatedRouteAuthorizer(self.store)
        self.deployment_coordinator = DeploymentCoordinator(
            self.store,
            live.runtime_control(),
        )

    def shutdown(self) -> None:
        executor = getattr(self, "_backtest_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    def list_definitions(self) -> dict[str, Any]:
        return {
            "definitions": [item.to_dict() for item in self.registry.list()],
            "quarantined": self.registry.quarantined(),
            "entry_point_group": "alphapilot.strategies",
        }

    def list_portfolio_policy_definitions(self) -> dict[str, Any]:
        return {
            "definitions": [item.to_dict() for item in self.policy_registry.list()],
            "quarantined": self.policy_registry.quarantined(),
            "entry_point_group": "alphapilot.portfolio_policies",
        }

    def list_instances(self) -> list[dict[str, Any]]:
        return self.store.list_instances()

    def create_instance(self, payload: dict[str, Any]) -> dict[str, Any]:
        definition = self.registry.get(str(payload.get("strategy_id") or ""))
        portfolio_policy = dict(payload.get("portfolio_policy") or {})
        if not portfolio_policy:
            portfolio_policy = self._default_portfolio_policy(definition.signal_kind, payload)
        else:
            portfolio_policy = self._bind_portfolio_policy(
                portfolio_policy,
                definition.signal_kind,
            )
        binding = dict(payload.get("artifact_binding") or {})
        config = StrategyInstanceConfig(
            instance_id=str(payload.get("instance_id") or ""),
            strategy_id=definition.strategy_id,
            strategy_version=str(payload.get("strategy_version") or definition.version),
            params=dict(payload.get("params") or {}),
            universe=tuple(payload.get("universe") or ()),
            frequency=str(payload.get("frequency") or "day"),
            data_policy=dict(
                payload.get("data_policy")
                or {"feature_adjustment": str(payload.get("adjust_mode") or "backward")}
            ),
            portfolio_policy=portfolio_policy,
            strategy_code_hash=definition.code_hash,
            model_hash=str(payload.get("model_hash") or binding.get("model_hash") or ""),
            artifact_binding=binding,
        )
        if config.strategy_version != definition.version:
            raise ValueError("strategy_version must match the registered definition")
        if definition.strategy_id == "qlib_selection":
            verify_artifact_binding(
                config.artifact_binding,
                snapshot_root=self.artifact_snapshotter.root,
                expected_instance_id=config.instance_id,
            )
        return self.store.create_instance(config)

    def create_instance_from_research_asset(self, payload: dict[str, Any]) -> dict[str, Any]:
        instance_id = str(payload.get("instance_id") or "").strip()
        strategy_name = str(payload.get("strategy_name") or payload.get("research_asset") or "").strip()
        if not instance_id or not strategy_name:
            raise ValueError("instance_id and strategy_name are required")
        binding = self.artifact_snapshotter.snapshot(
            strategy_name=strategy_name,
            instance_id=instance_id,
            universe=payload.get("universe"),
        )
        created = self.create_instance({
            "instance_id": instance_id,
            "strategy_id": "qlib_selection",
            "strategy_version": "1.0.0",
            "params": {},
            "universe": binding["universe"],
            "frequency": str(payload.get("frequency") or "day"),
            "data_policy": {
                "feature_adjustment": str(payload.get("adjust_mode") or "backward"),
                "data_version": str(payload.get("data_version") or binding.get("research_fingerprint") or ""),
            },
            "portfolio_policy": dict(payload.get("portfolio_policy") or {}),
            "model_hash": binding["model_hash"],
            "artifact_binding": binding,
        })
        self.store.record_artifact_manifest(
            instance_id,
            created["config_hash"],
            binding,
        )
        return created

    def update_instance(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.store.get_instance(instance_id)
        runtime = self.store.get_runtime_state(instance_id)
        editable_states = {
            LifecycleState.CREATED.value,
            LifecycleState.VALIDATED.value,
            LifecycleState.READY.value,
            LifecycleState.STOPPED.value,
        }
        if (
            runtime["binding_active"]
            or runtime["desired_state"] not in editable_states
            or runtime["observed_state"] not in editable_states
        ):
            raise ValueError(
                "a deployed strategy instance must be formally stopped before its "
                "parameters, universe, data or portfolio policy can change"
            )
        definition = self.registry.get(current["strategy_id"])
        safe_payload = {
            key: value for key, value in payload.items()
            if key in {"params", "universe", "frequency", "data_policy", "portfolio_policy"}
        }
        if "portfolio_policy" in safe_payload:
            safe_payload["portfolio_policy"] = self._bind_portfolio_policy(
                dict(safe_payload["portfolio_policy"] or {}),
                definition.signal_kind,
            )
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
        if config.deployment_level not in definition.deployable_modes:
            errors.append(
                f"deployment mode {config.deployment_level!r} is not supported by this provider"
            )
        policy_binding = dict(config.portfolio_policy or {})
        policy_id = str(policy_binding.get("policy_id") or "")
        try:
            policy_definition = self.policy_registry.get(policy_id)
        except KeyError as exc:
            errors.append(str(exc))
            policy_definition = None
        if policy_definition is not None:
            policy_version = str(policy_binding.get("version") or policy_definition.version)
            if policy_version != policy_definition.version:
                errors.append("portfolio policy version changed; update the instance")
            if not policy_definition.code_hash:
                errors.append("portfolio policy definition has no trusted code hash")
            elif str(policy_binding.get("code_hash") or "") != policy_definition.code_hash:
                errors.append("portfolio policy code hash changed; update the instance")
            if definition.signal_kind not in policy_definition.supported_signal_kinds:
                errors.append(
                    f"portfolio policy {policy_id!r} does not support {definition.signal_kind.value}"
                )
            errors.extend(
                validate_parameters(
                    policy_definition.parameter_schema,
                    dict(policy_binding.get("params") or {}),
                )
            )
        if config.strategy_id == "qlib_selection":
            try:
                verify_artifact_binding(
                    config.artifact_binding,
                    snapshot_root=self.artifact_snapshotter.root,
                    expected_instance_id=config.instance_id,
                )
            except (OSError, ValueError) as exc:
                errors.append(f"artifact binding invalid: {exc}")
            if config.model_hash != str(config.artifact_binding.get("model_hash") or ""):
                errors.append("model_hash does not match the immutable artifact binding")
            bound_universe = {
                str(item) for item in (config.artifact_binding.get("universe") or [])
            }
            if set(config.universe) != bound_universe:
                errors.append("instance universe does not match the immutable research artifact")
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
        deployment_errors = self._deployment_policy_errors(config, live.config.risk)
        return {
            "ok": True,
            "errors": [],
            "deployment_ready": not deployment_errors,
            "deployment_errors": deployment_errors,
            "instance": updated,
            "required_history": resolve_required_history(definition, config.params),
        }

    def backtest_instance(self, instance_id: str, payload: dict[str, Any]) -> Any:
        run_id = str(payload.get("run_id") or uuid.uuid4().hex)
        return self._run_replay(instance_id, run_id, payload)

    def preview_instance(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        validation = self.validate_instance(instance_id)
        if not validation["ok"]:
            raise ValueError("; ".join(validation["errors"]))
        config = StrategyInstanceConfig.from_dict(validation["instance"]["config"])
        timing = self.context.engine.get_system("timing")
        adjustment = str(
            payload.get("adjust_mode")
            or config.data_policy.get("feature_adjustment")
            or "backward"
        )
        frame = timing.load_bars(
            symbols=list(config.universe),
            start_date=payload.get("start_date"),
            end_date=payload.get("calendar_end_date"),
            freq=config.frequency,
            data_dir=payload.get("data_dir"),
            adjust_mode=adjustment,
        )
        sessions = sorted({pd.Timestamp(value).date().isoformat() for value in frame["datetime"]})
        if len(sessions) < 2:
            raise ValueError("signal preview requires an explicit next trading session")
        as_of = str(payload.get("as_of") or payload.get("end_date") or sessions[-2])[:10]
        evaluation_frame = frame[pd.to_datetime(frame["datetime"]).dt.date <= pd.Timestamp(as_of).date()]
        bars = completed_bars_from_frame(
            evaluation_frame,
            frequency=config.frequency,
            adjustment=PriceAdjustment(adjustment),
            data_version=str(config.data_policy.get("data_version") or ""),
        )
        account_payload = dict(payload.get("account") or {})
        account = AccountSnapshot(
            account_id=str(account_payload.get("account_id") or "preview"),
            as_of=as_of,
            balance=float(_option(
                account_payload,
                "balance",
                _option(payload, "cash", 100_000.0),
            )),
            available=float(_option(
                account_payload,
                "available",
                _option(payload, "cash", 100_000.0),
            )),
            positions={str(key): float(value) for key, value in (account_payload.get("positions") or {}).items()},
            sellable={str(key): float(value) for key, value in (account_payload.get("sellable") or {}).items()},
        )
        pipeline = DecisionPipeline(
            strategy_registry=self.registry,
            policy_registry=self.policy_registry,
            store=self.store,
            calendar=SequenceCalendar(sessions),
        )
        try:
            result = pipeline.evaluate(config, bars, account=account, persist=False)
        finally:
            pipeline.close("preview_complete")
        return {
            "instance_id": instance_id,
            "config_hash": config.config_hash,
            "signal": result.signal.to_dict(),
            "decision": result.decision.to_dict(),
            "persisted": False,
        }

    def start_backtest_run(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        validation = self.validate_instance(instance_id)
        if not validation["ok"]:
            raise ValueError("; ".join(validation["errors"]))
        run = self.store.create_backtest_run(instance_id, dict(payload))
        self._backtest_executor.submit(
            self._backtest_worker,
            instance_id,
            run["run_id"],
            dict(payload),
        )
        return run

    def get_backtest_run(self, run_id: str, *, detail: bool = False) -> dict[str, Any]:
        run = self.store.get_backtest_run(run_id)
        if detail and run["artifact_dir"]:
            root = Path(run["artifact_dir"])
            artifacts: dict[str, Any] = {}
            for name in (
                "manifest", "summary", "signals", "weights", "targets", "plans",
                "orders", "fills", "positions", "equity",
            ):
                path = root / f"{name}.json"
                if path.is_file():
                    artifacts[name] = json.loads(path.read_text(encoding="utf-8"))
            run["detail"] = artifacts
        return run

    def cancel_backtest_run(self, run_id: str) -> dict[str, Any]:
        return self.store.request_backtest_cancel(run_id)

    def _backtest_worker(
        self,
        instance_id: str,
        run_id: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            current = self.store.get_backtest_run(run_id)
            if current["cancel_requested"]:
                self.store.update_backtest_run(run_id, status="cancelled")
                return
            self.store.update_backtest_run(run_id, status="running")
            result = self._run_replay(instance_id, run_id, payload)
            current = self.store.get_backtest_run(run_id)
            if current["cancel_requested"]:
                self.store.update_backtest_run(
                    run_id,
                    status="cancelled",
                    result=result.summary,
                    artifact_dir=str(result.artifact_dir),
                )
            else:
                self.store.update_backtest_run(
                    run_id,
                    status="completed",
                    result=result.summary,
                    artifact_dir=str(result.artifact_dir),
                )
        except Exception as exc:  # noqa: BLE001 - persisted async failure
            self.store.update_backtest_run(
                run_id,
                status="failed",
                error={"type": type(exc).__name__, "message": str(exc)},
            )

    def _run_replay(
        self,
        instance_id: str,
        run_id: str,
        payload: dict[str, Any],
    ) -> Any:
        validation = self.validate_instance(instance_id)
        if not validation["ok"]:
            raise ValueError("; ".join(validation["errors"]))
        config = StrategyInstanceConfig.from_dict(validation["instance"]["config"])
        timing = self.context.engine.get_system("timing")
        feature_adjustment = str(
            payload.get("adjust_mode")
            or config.data_policy.get("feature_adjustment")
            or "backward"
        )
        common = {
            "symbols": list(config.universe),
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "freq": config.frequency,
            "data_dir": payload.get("data_dir"),
        }
        feature_frame = timing.load_bars(**common, adjust_mode=feature_adjustment)
        raw_data_dir = payload.get("execution_data_dir") or payload.get("data_dir")
        raw_frame = timing.load_bars(
            **{**common, "data_dir": raw_data_dir},
            adjust_mode="none",
        )
        feature_bars = completed_bars_from_frame(
            feature_frame,
            frequency=config.frequency,
            adjustment=PriceAdjustment(feature_adjustment),
            data_version=str(config.data_policy.get("data_version") or ""),
        )
        raw_bars = completed_bars_from_frame(
            raw_frame,
            frequency=config.frequency,
            adjustment=PriceAdjustment.NONE,
        )
        raw_data_version = str(payload.get("execution_data_version") or "")
        quote_overrides = tradable_quotes_from_frame(
            raw_frame,
            frequency=config.frequency,
            data_version=raw_data_version,
        )
        derived_metadata = instrument_metadata_from_frame(
            raw_frame,
            default_lot_size=int(_option(payload, "trade_unit", 100)),
        )
        result = ReplayRuntime(
            strategy_registry=self.registry,
            policy_registry=self.policy_registry,
            store=self.store,
            output_root=payload.get("output_dir") or self._backtest_output_root,
        ).run(
            run_id,
            config,
            feature_bars,
            raw_bars,
            config=ReplayConfig(
                initial_cash=float(_option(payload, "cash", 100_000.0)),
                open_cost=float(_option(payload, "open_cost", 0.0003)),
                close_cost=float(_option(payload, "close_cost", 0.0013)),
                min_cost=float(_option(payload, "min_cost", 5.0)),
                slippage=float(_option(payload, "slippage", 0.0)),
                lot_size=int(_option(payload, "trade_unit", 100)),
                max_order_value=float(_option(payload, "max_order_value", 0.0)),
                partial_fill_ratio=float(_option(payload, "partial_fill_ratio", 1.0)),
                instrument_metadata={
                    **derived_metadata,
                    **{
                        canonical_instrument(str(symbol)): InstrumentMetadata(
                            instrument=canonical_instrument(str(symbol)),
                            asset_type=str((values or {}).get("asset_type") or "equity"),
                            lot_size=int(_option(
                                values or {}, "lot_size", _option(payload, "trade_unit", 100),
                            )),
                            price_tick=float(_option(values or {}, "price_tick", 0.01)),
                            settlement_days=max(int((values or {}).get("settlement_days", 1)), 0),
                            long_only=bool((values or {}).get("long_only", True)),
                        )
                        for symbol, values in (payload.get("instrument_metadata") or {}).items()
                        if isinstance(values, dict)
                    },
                },
                quote_overrides=quote_overrides,
            ),
        )
        current = self.store.get_instance(instance_id)
        if current["config_hash"] != config.config_hash:
            raise RuntimeError("strategy instance changed during replay")
        if current["deployment_level"] == "replay":
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
        deployment_errors = self._deployment_policy_errors(config, live.config.risk)
        if deployment_errors:
            raise ValueError("; ".join(deployment_errors))
        current = self.store.get_instance(instance_id)
        target_level = str(payload.get("to") or "")
        if target_level == "live" and config.frequency != "day":
            raise ValueError("LIVE currently supports A-share/ETF daily strategies only")
        definition = self.registry.get(config.strategy_id)
        if target_level not in definition.deployable_modes:
            raise ValueError(f"strategy is not deployable to {target_level!r}")
        approval = str(payload.get("approval") or "")
        if target_level == "live" and not approval.startswith("apla_"):
            raise ValueError("LIVE promotion requires a one-time authorize-live approval")
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
            target_level,
            account_id=str(payload.get("account_id") or ""),
            broker=str(payload.get("broker") or ""),
            approval=approval,
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
        if not str(reason).strip():
            raise ValueError("kill switch changes require an operator reason")
        return self.store.set_route_block(
            scope_type,
            scope_id,
            active=active,
            reason=reason,
        )

    def list_kill_switches(self) -> list[dict[str, Any]]:
        return self.store.list_route_blocks()

    def create_operator_token(
        self,
        operator_id: str,
        *,
        label: str = "",
        expires_in_days: int | None = None,
    ) -> dict[str, Any]:
        return self.operator_auth.generate_token(
            operator_id,
            label=label,
            expires_in_days=expires_in_days,
        )

    def authorize_live(
        self,
        instance_id: str,
        payload: dict[str, Any],
        operator: Any,
    ) -> dict[str, Any]:
        current = self.store.get_instance(instance_id)
        if current["deployment_level"] != "shadow":
            raise ValueError("LIVE approval can be issued only from SHADOW")
        account_id = str(payload.get("account_id") or "")
        broker = str(payload.get("broker") or "")
        if not account_id or not broker:
            raise ValueError("account_id and broker are required")
        if payload.get("baseline_confirmed") is not True:
            raise ValueError("baseline_confirmed=true is required for a dedicated LIVE account")
        raw_baseline = payload.get("baseline_positions")
        if not isinstance(raw_baseline, dict):
            raise ValueError("baseline_positions must be the operator-confirmed account holdings")
        baseline = {
            canonical_instrument(str(key)): float(value)
            for key, value in raw_baseline.items() if float(value) != 0
        }
        outside = sorted(set(baseline) - set(current["config"].get("universe") or []))
        if outside:
            raise ValueError(f"baseline contains holdings outside the instance universe: {outside}")
        evidence = self.store.evaluate_stage(instance_id, "shadow", minimum_sessions=5)
        if not evidence["passed"]:
            raise ValueError("passing SHADOW evidence is required before LIVE authorization")
        self.store.save_account_baseline(
            instance_id,
            current["config_hash"],
            account_id,
            baseline,
            confirmed_by=operator.operator_id,
        )
        return self.operator_auth.issue_live_approval(
            operator,
            instance_id=instance_id,
            config_hash=current["config_hash"],
            account_id=account_id,
            broker=broker,
            reason=str(payload.get("reason") or operator.reason),
            ttl_seconds=int(_option(payload, "ttl_seconds", 300)),
        )

    def audit_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list_audit_events(limit=limit)

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
            changes: dict[str, Any] = {}
            if (
                config.strategy_version != definition.version
                or (definition.code_hash and config.strategy_code_hash != definition.code_hash)
            ):
                changes.update({
                    "strategy_version": definition.version,
                    "strategy_code_hash": definition.code_hash,
                })
            binding = dict(config.portfolio_policy or {})
            try:
                policy = self.policy_registry.get(str(binding.get("policy_id") or ""))
            except KeyError:
                policy = None
            if policy is not None and (
                str(binding.get("version") or "") != policy.version
                or str(binding.get("code_hash") or "") != policy.code_hash
            ):
                changes["portfolio_policy"] = {
                    **binding,
                    "version": policy.version,
                    "code_hash": policy.code_hash,
                }
            if changes:
                self.store.update_instance(row["instance_id"], changes)

    def _deployment_policy_errors(self, config: StrategyInstanceConfig, risk: Any) -> list[str]:
        errors: list[str] = []
        data_version = str(config.data_policy.get("data_version") or "").strip()
        if not data_version:
            errors.append(
                "data_policy.data_version is required before deployment so decisions remain reproducible"
            )
        try:
            PriceAdjustment(
                str(config.data_policy.get("feature_adjustment") or "backward")
            )
        except ValueError:
            errors.append("data_policy.feature_adjustment must be none, forward or backward")
        binding = dict(config.portfolio_policy or {})
        try:
            definition = self.policy_registry.get(str(binding.get("policy_id") or ""))
        except KeyError as exc:
            return [*errors, str(exc)]
        params = {
            key: spec["default"]
            for key, spec in (definition.parameter_schema.get("properties") or {}).items()
            if isinstance(spec, dict) and "default" in spec
        }
        params.update(dict(binding.get("params") or {}))
        configured_cap = float(
            params.get("target_percent", params.get("max_position_weight", 1.0)) or 0.0
        )
        risk_cap = float(risk.max_position_pct)
        if configured_cap > risk_cap + 1e-9:
            errors.append(
                f"portfolio policy per-instrument exposure {configured_cap:.1%} exceeds "
                f"automated max_position_pct {risk_cap:.1%}; set it to <= {risk_cap:.1%}"
            )
        return errors

    def _default_portfolio_policy(
        self,
        signal_kind: SignalKind,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if signal_kind == SignalKind.CROSS_SECTIONAL_SELECTION:
            definition = self.policy_registry.get("selection_topk_dropout_equal_weight")
            params = {
                "topk": int(_option(payload, "topk", 10)),
                "n_drop": int(_option(payload, "n_drop", 2)),
                "cash_buffer": float(_option(payload, "cash_buffer", 0.1)),
                "max_position_weight": float(_option(payload, "max_position_weight", 0.2)),
            }
        else:
            definition = self.policy_registry.get("timing_fixed_exposure")
            strategy_params = dict(payload.get("params") or {})
            params = {
                "target_percent": float(strategy_params.get("target_percent", 0.2)),
                "cash_buffer": float(_option(payload, "cash_buffer", 0.1)),
                "max_position_weight": float(_option(payload, "max_position_weight", 0.3)),
            }
        return {
            "policy_id": definition.policy_id,
            "version": definition.version,
            "params": params,
            "code_hash": definition.code_hash,
        }

    def _bind_portfolio_policy(
        self,
        binding: dict[str, Any],
        signal_kind: SignalKind,
    ) -> dict[str, Any]:
        """Resolve a public policy request to one immutable registry binding."""

        policy_id = str(binding.get("policy_id") or "").strip().lower()
        if not policy_id:
            raise ValueError("portfolio_policy.policy_id is required")
        definition = self.policy_registry.get(policy_id)
        requested_version = str(binding.get("version") or "")
        if requested_version and requested_version != definition.version:
            raise ValueError(
                f"portfolio policy {policy_id!r} version {requested_version!r} is not installed"
            )
        requested_hash = str(binding.get("code_hash") or "")
        if requested_hash and requested_hash != definition.code_hash:
            raise ValueError(f"portfolio policy {policy_id!r} code hash does not match")
        if signal_kind not in definition.supported_signal_kinds:
            raise ValueError(
                f"portfolio policy {policy_id!r} does not support {signal_kind.value}"
            )
        params = schema_defaults(definition.parameter_schema)
        params.update(dict(binding.get("params") or {}))
        errors = validate_parameters(definition.parameter_schema, params)
        if errors:
            raise ValueError("; ".join(errors))
        resolved = {
            "policy_id": definition.policy_id,
            "version": definition.version,
            "params": params,
            "code_hash": definition.code_hash,
        }
        if "constraints" in binding:
            resolved["constraints"] = dict(binding.get("constraints") or {})
        return resolved
