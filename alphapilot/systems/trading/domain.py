"""Stable strategy and portfolio-domain contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Protocol

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


class DeploymentLevel(str, Enum):
    REPLAY = "replay"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE = "live"


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
    deployable_modes: tuple[str, ...] = (
        DeploymentLevel.REPLAY.value,
        DeploymentLevel.PAPER.value,
        DeploymentLevel.SHADOW.value,
        DeploymentLevel.LIVE.value,
    )

    def __post_init__(self) -> None:
        self.signal_kind = SignalKind(self.signal_kind)
        self.provider_api_version = int(self.provider_api_version or self.api_version or 1)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["factory"] = factory_path(self.factory)
        data["signal_kind"] = self.signal_kind.value
        data["supported_assets"] = list(self.supported_assets)
        data["supported_frequencies"] = list(self.supported_frequencies)
        data["deployable_modes"] = list(self.deployable_modes)
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
    deployment_level: str = DeploymentLevel.REPLAY.value
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
