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

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["factory"] = factory_path(self.factory)
        data["supported_assets"] = list(self.supported_assets)
        data["supported_frequencies"] = list(self.supported_frequencies)
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
    deployment_level: str = DeploymentLevel.REPLAY.value
    config_hash: str = ""

    def __post_init__(self) -> None:
        self.instance_id = str(self.instance_id).strip()
        self.strategy_id = str(self.strategy_id).strip().lower()
        self.universe = tuple(str(item).strip() for item in self.universe if str(item).strip())
        if not self.config_hash:
            self.config_hash = self.compute_hash()

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
    def on_bars(self, completed_bars: Any) -> list[SignalRecord]: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, state: dict[str, Any]) -> None: ...
    def stop(self, reason: str) -> None: ...


def factory_path(factory: Any) -> str:
    if isinstance(factory, str):
        return factory
    module = getattr(factory, "__module__", "")
    qualname = getattr(factory, "__qualname__", getattr(factory, "__name__", ""))
    return f"{module}:{qualname}" if module and qualname else repr(factory)
