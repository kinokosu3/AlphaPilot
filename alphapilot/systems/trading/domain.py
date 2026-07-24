"""Stable strategy and portfolio-domain contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Protocol

from alphapilot.systems.trading.account_identity import account_identity_hash
from alphapilot.systems.trading.contracts import (
    CrossSectionalSignal,
    PortfolioInputs,
    PortfolioPolicy,
    SignalEnvelope,
    SignalKind,
    TargetWeights,
    TimingSignal,
)


class LifecycleState(str, Enum):
    CREATED = "created"
    VALIDATED = "validated"
    WARMING_UP = "warming_up"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    PAUSED_PENDING_RECONCILE = "paused_pending_reconcile"
    HALTED = "halted"
    ERROR = "error"
    STOPPED = "stopped"


class InstanceValidationState(str, Enum):
    CREATED = "created"
    VALIDATED = "validated"


class DeploymentMode(str, Enum):
    PAPER = "paper"
    SIMULATION = "simulation"
    SHADOW = "shadow"
    LIVE = "live"


class ExecutionEnvironment(str, Enum):
    """Physical execution environment selected by a deployment spec."""

    LOCAL_PAPER = "local_paper"
    BROKER_SIMULATION = "broker_simulation"
    LIVE = "live"


@dataclass(frozen=True)
class DeploymentSpec:
    instance_id: str
    config_hash: str
    run_mode: str
    execution_environment: str = ExecutionEnvironment.LOCAL_PAPER.value
    trade_provider: str = "paper"
    quote_provider: str = "paper"
    account_profile: str = ""
    account_id: str = ""
    quote_data_kind: str = "synthetic"
    binding_hash: str = ""

    def __post_init__(self) -> None:
        mode = DeploymentMode(self.run_mode).value
        environment = ExecutionEnvironment(self.execution_environment).value
        trade = str(self.trade_provider).strip().lower()
        quote = str(self.quote_provider).strip().lower()
        profile = str(self.account_profile).strip()
        account_id = str(self.account_id).strip()
        data_kind = str(self.quote_data_kind).strip().lower()
        if not self.instance_id:
            raise ValueError("instance_id is required")
        if not self.config_hash:
            raise ValueError("config_hash is required")
        if not trade or not quote:
            raise ValueError("trade_provider and quote_provider are required")
        if data_kind not in {"realtime", "replay", "synthetic"}:
            raise ValueError("quote_data_kind must be realtime, replay or synthetic")
        if mode == DeploymentMode.PAPER.value:
            if environment != ExecutionEnvironment.LOCAL_PAPER.value:
                raise ValueError("PAPER must use local_paper")
            if (trade, quote, data_kind) != ("paper", "paper", "synthetic"):
                raise ValueError("LOCAL_PAPER must use paper trade/quote with synthetic data")
            profile = ""
            account_id = ""
        elif mode == DeploymentMode.SIMULATION.value:
            if environment != ExecutionEnvironment.BROKER_SIMULATION.value:
                raise ValueError("SIMULATION must use broker_simulation")
            if not profile:
                raise ValueError("SIMULATION requires an account_profile alias")
            account_id = ""
        else:
            if environment != ExecutionEnvironment.LIVE.value:
                raise ValueError("SHADOW/LIVE must use the live execution environment")
            if data_kind != "realtime":
                raise ValueError("SHADOW/LIVE require realtime quote data")
            if not account_id:
                raise ValueError("SHADOW/LIVE require account_id")
            account_id = account_identity_hash(account_id)
            profile = ""
        object.__setattr__(self, "run_mode", mode)
        object.__setattr__(self, "execution_environment", environment)
        object.__setattr__(self, "trade_provider", trade)
        object.__setattr__(self, "quote_provider", quote)
        object.__setattr__(self, "account_profile", profile)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "quote_data_kind", data_kind)
        expected = self.compute_hash()
        if self.binding_hash and self.binding_hash != expected:
            raise ValueError("deployment binding_hash does not match its immutable fields")
        object.__setattr__(self, "binding_hash", expected)

    def compute_hash(self) -> str:
        payload = {
            "instance_id": self.instance_id,
            "config_hash": self.config_hash,
            "run_mode": self.run_mode,
            "execution_environment": self.execution_environment,
            "trade_provider": self.trade_provider,
            "quote_provider": self.quote_provider,
            "account_profile": self.account_profile,
            "account_id": self.account_id,
            "quote_data_kind": self.quote_data_kind,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class StrategyDefinition:
    strategy_id: str
    version: str
    kind: str
    factory: Any = field(repr=False)
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    supported_assets: tuple[str, ...] = ("equity", "fund")
    supported_frequencies: tuple[str, ...] = ("day", "min")
    output_type: str = "signals"
    required_history: int = 1
    state_schema_version: int = 1
    source: str = "builtin"
    package_version: str = ""
    code_hash: str = ""
    description: str = ""
    api_version: int = 1
    provider_api_version: int = 0
    signal_kind: SignalKind = SignalKind.INSTRUMENT_TIMING
    supported_run_modes: tuple[str, ...] = (
        DeploymentMode.PAPER.value,
        DeploymentMode.SIMULATION.value,
        DeploymentMode.SHADOW.value,
        DeploymentMode.LIVE.value,
    )

    def __post_init__(self) -> None:
        self.signal_kind = SignalKind(self.signal_kind)
        self.provider_api_version = int(self.provider_api_version or self.api_version or 1)
        self.supported_run_modes = tuple(
            str(item).strip().lower() for item in self.supported_run_modes
        )
        allowed_modes = {item.value for item in DeploymentMode}
        invalid_modes = set(self.supported_run_modes) - allowed_modes
        if invalid_modes:
            raise ValueError(
                f"unsupported strategy run modes: {sorted(invalid_modes)}; "
                f"expected a subset of {sorted(allowed_modes)}"
            )
        if not self.supported_run_modes:
            raise ValueError("supported_run_modes must not be empty")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["factory"] = factory_path(self.factory)
        data["signal_kind"] = self.signal_kind.value
        data["supported_assets"] = list(self.supported_assets)
        data["supported_frequencies"] = list(self.supported_frequencies)
        data["supported_run_modes"] = list(self.supported_run_modes)
        return data


@dataclass
class PortfolioPolicyDefinition:
    policy_id: str
    version: str
    factory: Any = field(repr=False)
    parameter_schema: dict[str, Any] = field(default_factory=dict)
    supported_signal_kinds: tuple[SignalKind, ...] = (
        SignalKind.INSTRUMENT_TIMING,
    )
    source: str = "builtin"
    package_version: str = ""
    code_hash: str = ""
    description: str = ""
    api_version: int = 1

    def __post_init__(self) -> None:
        self.policy_id = str(self.policy_id).strip().lower()
        self.supported_signal_kinds = tuple(
            SignalKind(item) for item in self.supported_signal_kinds
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["factory"] = factory_path(self.factory)
        data["supported_signal_kinds"] = [item.value for item in self.supported_signal_kinds]
        return data


@dataclass
class StrategyInstanceConfig:
    instance_id: str
    strategy_id: str
    strategy_version: str
    params: dict[str, Any] = field(default_factory=dict)
    universe: tuple[str, ...] = ()
    frequency: str = "day"
    data_policy: dict[str, Any] = field(default_factory=dict)
    portfolio_policy: dict[str, Any] = field(default_factory=dict)
    strategy_code_hash: str = ""
    model_hash: str = ""
    artifact_binding: dict[str, Any] = field(default_factory=dict)
    config_hash: str = ""

    def __post_init__(self) -> None:
        from alphapilot.systems.trading.contracts import canonical_instrument

        self.instance_id = str(self.instance_id).strip()
        self.strategy_id = str(self.strategy_id).strip().lower()
        self.universe = tuple(
            dict.fromkeys(
                canonical_instrument(str(item))
                for item in self.universe if str(item).strip()
            )
        )
        expected_hash = self.compute_hash()
        if self.config_hash and self.config_hash != expected_hash:
            raise ValueError("strategy instance config_hash does not match its immutable fields")
        self.config_hash = expected_hash

    def compute_hash(self) -> str:
        payload = {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "params": self.params,
            "universe": list(self.universe),
            "frequency": self.frequency,
            "data_policy": self.data_policy,
            "portfolio_policy": self.portfolio_policy,
            "strategy_code_hash": self.strategy_code_hash,
            "model_hash": self.model_hash,
            "artifact_binding": self.artifact_binding,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["universe"] = list(self.universe)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyInstanceConfig":
        return cls(**{**data, "universe": tuple(data.get("universe") or ())})


@dataclass(frozen=True)
class SignalRecord:
    instrument: str
    signal: int
    score: float = 0.0
    reason: str = ""


class SignalProvider(Protocol):
    def initialize(self, context: Any) -> None: ...
    def warmup(self, history: Any) -> None: ...
    def evaluate(self, context: Any) -> SignalEnvelope: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, state: dict[str, Any]) -> None: ...
    def stop(self, reason: str) -> None: ...


def factory_path(factory: Any) -> str:
    if isinstance(factory, str):
        return factory
    module = getattr(factory, "__module__", "")
    qualname = getattr(factory, "__qualname__", getattr(factory, "__name__", ""))
    return f"{module}:{qualname}" if module and qualname else repr(factory)
