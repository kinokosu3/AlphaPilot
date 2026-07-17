"""Kernel service for strategy definitions, instances and promotion gates."""

from __future__ import annotations

import os
import json
import hashlib
import socket
import csv
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import uuid
from typing import TYPE_CHECKING, Any

from alphapilot.kernel.base import BaseSystem
from alphapilot.kernel.registry import builtin_strategy_definitions
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
    LocalHistoricalDataAdapter,
    SequenceCalendar,
)
from alphapilot.systems.trading.replay import ReplayConfig, ReplayRuntime
from alphapilot.systems.trading.store import StrategyRuntimeStore
from alphapilot.systems.trading.operators import OperatorAuthService
from alphapilot.systems.trading.compatibility import (
    ENVIRONMENT_REPORT_SCHEMA,
    RemovalReadinessService,
    compatibility_matrix,
    compatibility_environment_report_hash,
    register_manifest as register_compatibility_manifest,
    validate_compatibility_environment_report,
)
from alphapilot.systems.trading.parity import (
    DecisionParityService,
    DeploymentQualificationService,
)

if TYPE_CHECKING:
    from alphapilot.kernel.context import Context


def _option(payload: dict[str, Any], key: str, default: Any) -> Any:
    value = payload.get(key)
    return default if value is None or value == "" else value


def _utc_timestamp(value: str) -> datetime | None:
    try:
        candidate = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    return candidate.astimezone(timezone.utc)


def _latest_cutoff(values: list[str]) -> str:
    parsed = [item for value in values if (item := _utc_timestamp(value)) is not None]
    return max(parsed).isoformat(timespec="seconds") if parsed else ""


def _uat_evidence_matches_installed_artifacts(
    evidence: dict[str, Any],
    metadata: dict[str, Any],
) -> bool:
    """Accept UAT evidence only when the installed artifacts are unchanged.

    UAT schema v8 stored ``code_commit`` both as its own audit field and inside
    ``plugin_hash``.  A later documentation/API-only commit therefore changed
    the computed plugin hash even when the plugin, native SDK and live routing
    core were byte-for-byte identical.  Rebuild the historical fingerprint
    with the evidence commit so that only this redundant commit component may
    differ; every actual artifact fingerprint remains mandatory.
    """

    for field in (
        "plugin_version",
        "sdk_version",
        "sdk_hash",
        "runtime_code_hash",
    ):
        if str(evidence.get(field) or "") != str(metadata.get(field) or ""):
            return False
    persisted_hash = str(evidence.get("plugin_hash") or "")
    if not persisted_hash:
        return False
    if persisted_hash == str(metadata.get("plugin_hash") or ""):
        return True
    evidence_commit = str(evidence.get("code_commit") or "").strip().lower()
    if len(evidence_commit) != 40 or any(
        character not in "0123456789abcdef" for character in evidence_commit
    ):
        return False
    historical_fingerprint = {
        key: value for key, value in metadata.items() if key != "plugin_hash"
    }
    historical_fingerprint["code_commit"] = evidence_commit
    historical_hash = hashlib.sha256(
        json.dumps(
            historical_fingerprint,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return persisted_hash == historical_hash


class TradingStrategySystem(BaseSystem):
    name = "trading"

    def setup(self, context: "Context") -> None:
        self.context = context
        local_root = os.getenv("ALPHAPILOT_STRATEGY_DIR") or str(Path.cwd() / "strategies")
        self.registry = StrategyRegistry(local_root=local_root).discover(
            builtin_contributions=builtin_strategy_definitions(),
        )
        policy_root = os.getenv("ALPHAPILOT_PORTFOLIO_POLICY_DIR") or str(Path.cwd() / "policies")
        self.policy_registry = PortfolioPolicyRegistry(local_root=policy_root).discover()
        self.historical_data = LocalHistoricalDataAdapter(context)
        self.historical_execution_data = self.historical_data
        live = context.engine.get_system("live")
        self.store = StrategyRuntimeStore(Path(live.config.state_dir) / "strategy_runtime.sqlite3")
        configured_environment = str(os.getenv("ALPHAPILOT_ENVIRONMENT_ID") or "").strip()
        environment_material = configured_environment or (
            f"{socket.gethostname()}:{Path(live.config.state_dir).expanduser().resolve()}"
        )
        self.compatibility_environment_id = hashlib.sha256(
            environment_material.encode("utf-8")
        ).hexdigest()[:24]
        self.store.register_compatibility_environment(self.compatibility_environment_id)
        register_compatibility_manifest(self.store)
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
        self.parity_service = DecisionParityService(self.store)
        self.qualification_service = DeploymentQualificationService(self.store)
        self.removal_readiness_service = RemovalReadinessService(
            self.store,
            repository_root=Path.cwd(),
        )
        self.broker_uat_harness = live.broker_uat_harness(self.store)
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

    def build_instance_config(self, payload: dict[str, Any]) -> StrategyInstanceConfig:
        """Normalize and validate an instance without choosing a persistence adapter."""

        definition = self.registry.get(str(payload.get("strategy_id") or ""))
        strategy_params = dict(payload.get("params") or {})
        legacy_target = None
        if definition.signal_kind == SignalKind.INSTRUMENT_TIMING:
            legacy_target = strategy_params.pop("target_percent", None)
        normalized_payload = {**payload, "params": strategy_params}
        portfolio_policy = dict(payload.get("portfolio_policy") or {})
        if not portfolio_policy:
            policy_payload = dict(normalized_payload)
            if legacy_target is not None:
                policy_payload["target_percent"] = legacy_target
            portfolio_policy = self._default_portfolio_policy(
                definition.signal_kind,
                policy_payload,
            )
        else:
            if legacy_target is not None:
                policy_params = dict(portfolio_policy.get("params") or {})
                policy_params.setdefault("target_percent", float(legacy_target))
                portfolio_policy["params"] = policy_params
            portfolio_policy = self._bind_portfolio_policy(
                portfolio_policy,
                definition.signal_kind,
            )
        binding = dict(payload.get("artifact_binding") or {})
        data_policy = dict(
            payload.get("data_policy")
            or {"feature_adjustment": str(payload.get("adjust_mode") or "backward")}
        )
        data_policy.setdefault(
            "history_window",
            resolve_required_history(definition, strategy_params),
        )
        config = StrategyInstanceConfig(
            instance_id=str(payload.get("instance_id") or ""),
            strategy_id=definition.strategy_id,
            strategy_version=str(payload.get("strategy_version") or definition.version),
            params=strategy_params,
            universe=tuple(payload.get("universe") or ()),
            frequency=str(payload.get("frequency") or "day"),
            data_policy=data_policy,
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
        return config

    def create_instance(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = self.build_instance_config(payload)
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
                "risk_policy": dict(payload.get("risk_policy") or {}),
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
        next_params = dict(
            safe_payload.get("params")
            if "params" in safe_payload else current["config"].get("params") or {}
        )
        legacy_target = None
        if definition.signal_kind == SignalKind.INSTRUMENT_TIMING:
            legacy_target = next_params.pop("target_percent", None)
            if "params" in safe_payload:
                safe_payload["params"] = next_params
        if "portfolio_policy" in safe_payload:
            policy_request = dict(safe_payload["portfolio_policy"] or {})
            if legacy_target is not None:
                policy_params = dict(policy_request.get("params") or {})
                policy_params.setdefault("target_percent", float(legacy_target))
                policy_request["params"] = policy_params
            safe_payload["portfolio_policy"] = self._bind_portfolio_policy(
                policy_request,
                definition.signal_kind,
            )
        elif legacy_target is not None:
            policy_request = dict(current["config"].get("portfolio_policy") or {})
            policy_params = dict(policy_request.get("params") or {})
            policy_params["target_percent"] = float(legacy_target)
            policy_request["params"] = policy_params
            safe_payload["portfolio_policy"] = self._bind_portfolio_policy(
                policy_request,
                definition.signal_kind,
            )
        next_data_policy = dict(
            safe_payload.get("data_policy")
            if "data_policy" in safe_payload else current["config"].get("data_policy") or {}
        )
        resolved_history = resolve_required_history(definition, next_params)
        if "params" in safe_payload and "data_policy" not in safe_payload:
            # Re-resolve the generated default when strategy parameters change;
            # an explicitly supplied data_policy remains an immutable operator choice.
            next_data_policy["history_window"] = resolved_history
        else:
            next_data_policy.setdefault("history_window", resolved_history)
        safe_payload["data_policy"] = next_data_policy
        safe_payload["strategy_code_hash"] = definition.code_hash
        return self.store.update_instance(instance_id, safe_payload)

    def validate_instance_config(
        self,
        config: StrategyInstanceConfig,
    ) -> dict[str, Any]:
        """Validate a normalized config without reading or mutating persistence."""

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
        required_history = resolve_required_history(definition, config.params)
        history_window = int(config.data_policy.get("history_window") or 0)
        if history_window < required_history:
            errors.append(
                f"data_policy.history_window must be >= {required_history} for this instance"
            )
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
        live = self.context.engine.get_system("live")
        deployment_errors = self._deployment_policy_errors(config, live.config.risk)
        return {
            "ok": not errors,
            "errors": errors,
            "deployment_ready": not deployment_errors,
            "deployment_errors": deployment_errors,
            "required_history": required_history,
            "history_window": history_window,
        }

    def validate_instance(self, instance_id: str) -> dict[str, Any]:
        row = self.store.get_instance(instance_id)
        config = StrategyInstanceConfig.from_dict(row["config"])
        validation = self.validate_instance_config(config)
        if not validation["ok"]:
            return {**validation, "instance": row}
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
        return {
            **validation,
            "instance": updated,
        }

    def backtest_instance(self, instance_id: str, payload: dict[str, Any]) -> Any:
        run_id = str(payload.get("run_id") or uuid.uuid4().hex)
        return self._run_replay(instance_id, run_id, payload)

    def preview_instance(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        validation = self.validate_instance(instance_id)
        if not validation["ok"]:
            raise ValueError("; ".join(validation["errors"]))
        config = StrategyInstanceConfig.from_dict(validation["instance"]["config"])
        adjustment = str(
            payload.get("adjust_mode")
            or config.data_policy.get("feature_adjustment")
            or "backward"
        )
        bars = self.historical_data.load_completed_bars(
            instruments=list(config.universe),
            start=payload.get("start_date"),
            end=payload.get("calendar_end_date"),
            frequency=config.frequency,
            data_dir=payload.get("data_dir") or config.data_policy.get("data_dir"),
            adjustment=adjustment,
        )
        sessions = sorted({str(bar.datetime)[:10] for bar in bars})
        if len(sessions) < 2:
            raise ValueError("signal preview requires an explicit next trading session")
        as_of = str(payload.get("as_of") or payload.get("end_date") or sessions[-2])[:10]
        bars = [bar for bar in bars if str(bar.datetime)[:10] <= as_of]
        configured_version = str(config.data_policy.get("data_version") or "")
        if configured_version:
            bars = [
                type(bar).from_dict({**bar.to_dict(), "data_version": configured_version})
                for bar in bars
            ]
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
            if run.get("origin") == "legacy_import":
                for name, filename in (
                    ("signals", "signals.csv"),
                    ("fills", "trades.csv"),
                    ("positions", "positions.csv"),
                    ("equity", "equity_curve.csv"),
                ):
                    path = root / filename
                    if name not in artifacts and path.is_file():
                        with path.open("r", encoding="utf-8-sig", newline="") as stream:
                            artifacts[name] = list(csv.DictReader(stream))
                migration = root / "compatibility_migration.json"
                if migration.is_file():
                    artifacts["compatibility_migration"] = json.loads(
                        migration.read_text(encoding="utf-8")
                    )
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
        feature_adjustment = str(
            payload.get("adjust_mode")
            or config.data_policy.get("feature_adjustment")
            or "backward"
        )
        data_dir = payload.get("data_dir") or config.data_policy.get("data_dir")
        feature_bars = self.historical_data.load_completed_bars(
            instruments=list(config.universe),
            start=payload.get("start_date"),
            end=payload.get("end_date"),
            frequency=config.frequency,
            adjustment=feature_adjustment,
            data_dir=data_dir,
        )
        configured_version = str(config.data_policy.get("data_version") or "")
        if configured_version:
            feature_bars = [
                type(bar).from_dict({**bar.to_dict(), "data_version": configured_version})
                for bar in feature_bars
            ]
        execution_slice = self.historical_execution_data.load_execution_slice(
            instruments=list(config.universe),
            start=payload.get("start_date"),
            end=payload.get("end_date"),
            frequency=config.frequency,
            data_dir=(
                payload.get("execution_data_dir")
                or config.data_policy.get("execution_data_dir")
                or data_dir
            ),
            default_lot_size=int(_option(payload, "trade_unit", 100)),
        )
        raw_bars = list(execution_slice.bars)
        quote_overrides = dict(execution_slice.quotes)
        derived_metadata = dict(execution_slice.instruments)
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
        # A replay has its own journal so that it is reproducible and can be
        # inspected independently.  Publish only its immutable observations to
        # the control store; deployment parity must never depend on opening an
        # arbitrary artifact database supplied by a caller.
        replay_store = StrategyRuntimeStore(Path(result.artifact_dir) / "runtime.sqlite3")
        for observation in replay_store.list_decision_observations(
            instance_id,
            mode="replay",
            run_id=run_id,
        ):
            self.store.record_decision_observation(observation)
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
        if bool(config.data_policy.get("compatibility_only")):
            raise ValueError("legacy compatibility instances are REPLAY-only and cannot be promoted")
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
        qualification = self._qualification(
            instance_id,
            account_id=account_id,
            broker=broker,
        )
        if not qualification["eligible_for_live_authorization"]:
            failed = [
                name for name in (
                    "paper", "shadow", "parity", "broker_uat", "reconcile", "configuration",
                )
                if not bool(qualification[name]["passed"])
            ]
            raise ValueError(
                "LIVE qualification is incomplete: " + ", ".join(failed)
            )
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

    def compatibility_status(self) -> dict[str, Any]:
        local_report = self._refresh_local_compatibility_report()
        matrix = compatibility_matrix()
        matrix_by_entrypoint = {item["entrypoint"]: item for item in matrix}
        entrypoints = [
            {
                **row,
                "equivalence": matrix_by_entrypoint.get(str(row["entrypoint"]), {}),
            }
            for row in self.store.compatibility_status()
        ]
        return {
            "schema_version": self.store.schema_version,
            "environment_id": self.compatibility_environment_id,
            "environments": self.store.compatibility_environment_status(),
            "entrypoints": entrypoints,
            "equivalence_matrix": matrix,
            "timing_equivalence": self._timing_equivalence_status(),
            "local_environment_report": local_report,
        }

    def _refresh_local_compatibility_report(self) -> dict[str, Any]:
        environment = self.store.compatibility_environment_status(
            self.compatibility_environment_id
        )
        cutoff = str(environment.get("migration_cutoff") or "")
        if not cutoff:
            return {
                "ready": False,
                "environment_id": self.compatibility_environment_id,
                "error": "migration cutoff is not set",
            }
        entrypoints = self.store.compatibility_environment_entrypoints(
            self.compatibility_environment_id
        )
        runtime_blockers = self.store.legacy_runtime_blockers()
        unmigrated_jobs = self.removal_readiness_service._unmigrated_legacy_jobs()
        payload: dict[str, Any] = {
            "schema_version": ENVIRONMENT_REPORT_SCHEMA,
            "runtime_schema_version": self.store.schema_version,
            "environment_id": self.compatibility_environment_id,
            "migration_cutoff": cutoff,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "code_commit": str(self.removal_readiness_service._git_state().get("commit") or ""),
            "post_cutoff_count": sum(
                int(item.get("post_cutoff_count") or 0) for item in entrypoints
            ),
            "active_legacy_runtime_count": sum(
                len(items) for items in runtime_blockers.values()
            ),
            "unmigrated_legacy_job_count": len(unmigrated_jobs),
            "entrypoints": entrypoints,
        }
        payload["evidence_hash"] = compatibility_environment_report_hash(payload)
        self.store.save_compatibility_environment_report(
            payload,
            evidence_hash=payload["evidence_hash"],
            imported=False,
        )
        return {"ready": True, **payload}

    def import_compatibility_environment_report(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        report = validate_compatibility_environment_report(payload)
        if report["environment_id"] == self.compatibility_environment_id:
            raise ValueError("the local compatibility report cannot be re-imported")
        return self.store.save_compatibility_environment_report(
            report,
            evidence_hash=str(report["evidence_hash"]),
            imported=True,
        )

    def _timing_equivalence_status(self) -> dict[str, Any]:
        required = sorted(
            definition.strategy_id
            for definition in self.registry.list()
            if definition.signal_kind == SignalKind.INSTRUMENT_TIMING
        )
        coverage = {
            strategy_id: {"passed_runs": 0, "cases": []}
            for strategy_id in required
        }
        global_cases: set[str] = set()
        for run in self.store.list_legacy_compatibility_runs():
            strategy_id = str(run["strategy_id"])
            if strategy_id not in coverage or run["status"] != "completed":
                continue
            equivalence = dict(run.get("result", {}).get("compatibility_equivalence") or {})
            instance_config = dict(run.get("instance_config") or {})
            try:
                current_code_hash = self.registry.get(strategy_id).code_hash
            except KeyError:
                continue
            if (
                equivalence.get("status") != "passed"
                or str(instance_config.get("strategy_code_hash") or "") != current_code_hash
            ):
                continue
            cases: set[str] = set()
            universe = list(instance_config.get("universe") or [])
            cases.add("multi_instrument" if len(universe) > 1 else "single_instrument")
            request = dict(run.get("request") or {})
            if request.get("start_date") or request.get("end_date"):
                cases.add("date_filter")
            target = float(
                (instance_config.get("portfolio_policy") or {}).get("params", {}).get(
                    "target_percent", 0.0,
                )
            )
            if abs(target) < 1e-12:
                cases.add("target_0")
            if abs(target - 0.2) < 1e-12:
                cases.add("target_20")
            if abs(target - 1.0) < 1e-12:
                cases.add("target_100")
            coverage[strategy_id]["passed_runs"] += 1
            coverage[strategy_id]["cases"] = sorted(
                set(coverage[strategy_id]["cases"]) | cases
            )
            global_cases.update(cases)
        required_cases = {
            "single_instrument", "multi_instrument", "date_filter",
            "target_0", "target_20", "target_100",
        }
        missing_strategies = sorted(
            strategy_id for strategy_id, item in coverage.items()
            if not item["passed_runs"]
        )
        return {
            "passed": not missing_strategies and required_cases <= global_cases,
            "required_strategies": required,
            "missing_strategies": missing_strategies,
            "required_cases": sorted(required_cases),
            "covered_cases": sorted(global_cases),
            "missing_cases": sorted(required_cases - global_cases),
            "coverage": coverage,
        }

    def set_compatibility_cutoff(self) -> dict[str, Any]:
        cutoff = self.store.set_compatibility_cutoff()
        return {"migration_cutoff": cutoff, **self.compatibility_status()}

    def start_parity_run(self, instance_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        replay_run_id = str(payload.get("replay_run_id") or "").strip()
        shadow_stage_run_id = str(payload.get("shadow_stage_run_id") or "").strip()
        if not replay_run_id or not shadow_stage_run_id:
            raise ValueError("replay_run_id and shadow_stage_run_id are required")
        replay = self.store.get_backtest_run(replay_run_id)
        shadow = self.store.get_stage_run(shadow_stage_run_id)
        if replay["instance_id"] != instance_id or shadow["instance_id"] != instance_id:
            raise ValueError("parity inputs must belong to the requested instance")
        if replay["status"] != "completed" or shadow["status"] != "completed":
            raise ValueError("parity inputs must be completed runs")
        if shadow["stage"] != "shadow":
            raise ValueError("shadow_stage_run_id must identify a SHADOW run")
        current = self.store.get_instance(instance_id)
        if (
            replay["config_hash"] != current["config_hash"]
            or shadow["config_hash"] != current["config_hash"]
        ):
            raise ValueError("parity input config_hash is stale")
        return self.parity_service.compare(
            instance_id,
            replay_run_id=replay_run_id,
            shadow_stage_run_id=shadow_stage_run_id,
        )

    def get_parity_run(self, run_id: str) -> dict[str, Any]:
        return self.store.get_parity_run(run_id)

    def qualification(
        self,
        instance_id: str,
        *,
        account_id: str = "",
        broker: str = "",
    ) -> dict[str, Any]:
        """Return mechanically derived promotion evidence.

        ``account_id`` and ``broker`` are optional because a SHADOW instance is
        deliberately not account-bound yet.  Supplying them lets the caller
        validate the exact XTP UAT evidence before asking for LIVE approval;
        the raw account identifier is never included in the result.
        """

        return self._qualification(
            instance_id,
            account_id=account_id,
            broker=broker,
        )

    def _qualification(
        self,
        instance_id: str,
        *,
        account_id: str = "",
        broker: str = "",
    ) -> dict[str, Any]:
        runtime = self.store.get_runtime_state(instance_id)
        selected_broker = str(broker or runtime.get("broker") or "").lower()
        metadata: dict[str, Any] = {}
        metadata_error = ""
        if selected_broker in {"xtp", "emt"}:
            try:
                metadata = self.broker_uat_harness.plugin_metadata(selected_broker)
            except Exception as exc:  # noqa: BLE001 - qualification remains fail closed
                metadata_error = f"{type(exc).__name__}: {exc}"
        result = self.qualification_service.evaluate(
            instance_id,
            account_id=account_id,
            broker=selected_broker,
            environment=str(os.getenv("ALPHAPILOT_BROKER_UAT_ENVIRONMENT") or ""),
            plugin_version=str(metadata.get("plugin_version") or "__unavailable__")
            if selected_broker in {"xtp", "emt"} else "",
            plugin_hash=str(metadata.get("plugin_hash") or "__unavailable__")
            if selected_broker in {"xtp", "emt"} else "",
            sdk_version=str(metadata.get("sdk_version") or "__unavailable__")
            if selected_broker in {"xtp", "emt"} else "",
            sdk_hash=str(metadata.get("sdk_hash") or "__unavailable__")
            if selected_broker in {"xtp", "emt"} else "",
            runtime_code_hash=str(metadata.get("runtime_code_hash") or "__unavailable__")
            if selected_broker in {"xtp", "emt"} else "",
        )
        result["broker_uat"]["plugin_metadata_error"] = metadata_error
        return result

    def list_broker_uat_runs(self, broker: str | None = None) -> list[dict[str, Any]]:
        return self.store.list_broker_uat_runs(broker)

    def get_broker_uat_run(self, run_id: str) -> dict[str, Any]:
        return self.store.get_broker_uat_run(run_id)

    def broker_uat_preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.broker_uat_harness.preflight(
            broker=str(payload.get("broker") or ""),
            symbols=[str(item) for item in payload.get("symbols") or []],
            max_notional=float(_option(payload, "max_notional", 20_000.0)),
            timeout=float(_option(payload, "timeout", 30.0)),
        )

    def start_broker_uat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.broker_uat_harness.start(
            broker=str(payload.get("broker") or ""),
            symbol=str(payload.get("symbol") or ""),
            side=str(payload.get("side") or ""),
            volume=float(payload.get("volume") or 0.0),
            price=float(payload.get("price") or 0.0),
            max_notional=float(payload.get("max_notional") or 0.0),
            confirmation=str(payload.get("confirmation") or ""),
            timeout=float(_option(payload, "timeout", 30.0)),
        )

    def resume_broker_uat(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.broker_uat_harness.resume(
            run_id,
            confirmation=str(payload.get("confirmation") or ""),
            timeout=float(_option(payload, "timeout", 30.0)),
        )

    def abort_broker_uat(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.broker_uat_harness.abort(
            run_id,
            confirmation=str(payload.get("confirmation") or ""),
            reason=str(payload.get("reason") or "operator aborted Broker UAT"),
        )

    def removal_check(self, acceptance_instance_id: str) -> dict[str, Any]:
        if not str(acceptance_instance_id).strip():
            raise ValueError("acceptance_instance_id is required")
        # Removal readiness is deliberately independent from the 20/5-day LIVE
        # promotion gate.  It proves replacement equivalence and operational
        # safety; it never grants automated LIVE authority.
        self.compatibility_status()
        report = self.removal_readiness_service.evaluate(acceptance_instance_id)
        live_qualification = self._qualification(acceptance_instance_id)
        report["live_qualification"] = live_qualification
        equivalence = self._timing_equivalence_status()
        report["timing_equivalence"] = equivalence
        report["checks"]["timing_equivalence_matrix"] = bool(equivalence["passed"])
        observation_cutoff = _latest_cutoff([
            str(row.get("migration_cutoff") or "")
            for row in report.get("environments", [])
        ])
        report["removal_qualification"] = {
            "observation_cutoff": observation_cutoff,
            "timing_equivalence": bool(equivalence["passed"]),
            "live_waiting_period_required": False,
        }
        broker_evidence: dict[str, dict[str, Any] | None] = {}
        for broker in ("xtp", "emt"):
            try:
                metadata = self.broker_uat_harness.plugin_metadata(broker)
                evidence = self.store.valid_broker_uat_evidence(
                    broker,
                    environment=str(
                        os.getenv("ALPHAPILOT_BROKER_UAT_ENVIRONMENT") or ""
                    ).strip(),
                    plugin_version=metadata["plugin_version"],
                    sdk_version=metadata["sdk_version"],
                    sdk_hash=metadata["sdk_hash"],
                    runtime_code_hash=metadata["runtime_code_hash"],
                    scenario_version=2,
                    passed_after=observation_cutoff,
                )
                if evidence is not None and not _uat_evidence_matches_installed_artifacts(
                    evidence,
                    metadata,
                ):
                    evidence = None
            except Exception:  # noqa: BLE001 - missing plugin/SDK invalidates UAT proof
                evidence = None
            broker_evidence[broker] = evidence
            report["checks"][f"{broker}_uat"] = evidence is not None
            report["broker_uat"][broker] = (
                None if evidence is None else {
                    "evidence_id": evidence["evidence_id"],
                    "evidence_hash": evidence["evidence_hash"],
                    "environment": evidence["environment"],
                    "plugin_version": evidence["plugin_version"],
                    "sdk_hash": evidence["sdk_hash"],
                    "runtime_code_hash": evidence["runtime_code_hash"],
                    "passed_at": evidence["passed_at"],
                    "expires_at": evidence["expires_at"],
                }
            )
        completed_at = _latest_cutoff([
            *[
                str(evidence.get("passed_at") or "")
                for evidence in broker_evidence.values() if evidence is not None
            ],
        ])
        environment_reports_after_cycle = bool(completed_at) and all(
            (
                (generated := _utc_timestamp(
                    str((row.get("evidence") or {}).get("generated_at") or "")
                )) is not None
                and (completed := _utc_timestamp(completed_at)) is not None
                and generated >= completed
            )
            for row in report.get("environments", [])
        )
        report["removal_qualification"]["completed_at"] = completed_at
        report["checks"]["environment_reports_after_acceptance_cycle"] = (
            environment_reports_after_cycle
        )
        report["ready"] = all(report["checks"].values())
        report["removal_qualification"]["eligible_for_removal"] = report["ready"]
        evidence_material = {
            "commit": report.get("code", {}).get("commit", ""),
            "schema_version": self.store.schema_version,
            "acceptance_config_hash": live_qualification.get("config_hash", ""),
            "observation_cutoff": observation_cutoff,
            "removal_acceptance_completed_at": completed_at,
            "release_verification_hash": str(
                (report.get("release_verification") or {}).get("report_hash") or ""
            ),
            "environment_reports": {
                str(row["environment_id"]): str(row.get("evidence_hash") or "")
                for row in report.get("environments", [])
            },
            "broker_uat": {
                broker: str((report["broker_uat"].get(broker) or {}).get("evidence_hash") or "")
                for broker in ("xtp", "emt")
            },
        }
        report["evidence_hash"] = hashlib.sha256(
            json.dumps(evidence_material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        report.pop("report_hash", None)
        report["report_hash"] = hashlib.sha256(
            json.dumps(report, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return report

    def start_stage_run(self, instance_id: str, stage: str) -> dict[str, Any]:
        if stage not in {"paper", "shadow", "live"}:
            raise ValueError("stage run evidence is supported only for PAPER, SHADOW and LIVE")
        return self.store.start_stage_run(instance_id, stage)

    def finish_stage_run(self, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.finish_stage_run(
            run_id,
            trading_sessions=int(payload.get("trading_sessions") or 0),
            metrics=dict(payload.get("metrics") or {}),
            status=str(payload.get("status") or "completed"),
        )

    def evaluate_stage(self, instance_id: str, stage: str) -> dict[str, Any]:
        minimums = {"paper": 20, "shadow": 5, "live": 5}
        if stage not in minimums:
            raise ValueError("only PAPER, SHADOW and LIVE have mechanical stage-run gates")
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
        bound_risk = dict(config.data_policy.get("risk_policy") or {})
        for key, expected in sorted(bound_risk.items()):
            if not hasattr(risk, key):
                errors.append(f"bound risk policy uses unsupported limit {key!r}")
                continue
            actual = getattr(risk, key)
            if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
                matches = abs(float(actual) - float(expected)) <= 1e-12
            else:
                matches = actual == expected
            if not matches:
                errors.append(
                    f"runtime risk limit {key}={actual!r} does not match "
                    f"immutable instance binding {expected!r}"
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
            target = float(_option(
                payload,
                "target_percent",
                strategy_params.get("target_percent", 0.2),
            ))
            params = {
                "target_percent": target,
                "cash_buffer": float(_option(payload, "cash_buffer", 0.1)),
                "max_position_weight": float(
                    _option(payload, "max_position_weight", max(0.3, target))
                ),
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
