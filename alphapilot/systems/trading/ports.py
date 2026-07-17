"""Application ports shared with concrete live-runtime adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Mapping, Protocol, Sequence

from alphapilot.systems.trading.contracts import (
    AccountSnapshot,
    CompletedBar,
    InstrumentMetadata,
    TradableQuote,
    ExecutionChild,
)


class RouteOrigin(str, Enum):
    MANUAL = "manual"
    AUTOMATED = "automated"
    BROKER_UAT = "broker_uat"


@dataclass(frozen=True)
class RouteContext:
    """Trusted metadata attached by an execution entry point.

    Strategy code receives an ``AutomatedOrderRoutePort`` and never constructs
    this context itself.  Operator APIs create a manual context explicitly.
    """

    origin: RouteOrigin
    instance_id: str = ""
    config_hash: str = ""
    account_id: str = ""
    broker: str = ""
    deployment_level: str = ""
    runtime_id: str = ""
    execution_environment: str = ""
    trade_provider: str = ""
    quote_provider: str = ""
    quote_data_kind: str = ""
    binding_hash: str = ""
    uat_run_id: str = ""

    @classmethod
    def manual(cls) -> "RouteContext":
        return cls(origin=RouteOrigin.MANUAL)


@dataclass(frozen=True)
class RouteAuthorization:
    allowed: bool
    rule: str
    reason: str = ""
    checked_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())


class AutomatedRouteAuthorizerPort(Protocol):
    def authorize(self, context: RouteContext) -> RouteAuthorization: ...


class AutomatedOrderRoutePort(Protocol):
    def submit(self, request: Any) -> str | None: ...


class HistoricalDataPort(Protocol):
    def load_completed_bars(
        self,
        *,
        instruments: Sequence[str],
        start: str | None,
        end: str | None,
        frequency: str,
        adjustment: str,
        data_dir: str | None = None,
    ) -> list[CompletedBar]: ...


@dataclass(frozen=True)
class HistoricalExecutionSlice:
    bars: tuple[CompletedBar, ...]
    quotes: Mapping[str, Mapping[str, TradableQuote]]
    instruments: Mapping[str, InstrumentMetadata]
    data_version: str = ""


class HistoricalExecutionDataPort(Protocol):
    def load_execution_slice(
        self,
        *,
        instruments: Sequence[str],
        start: str | None,
        end: str | None,
        frequency: str,
        data_dir: str | None = None,
        default_lot_size: int = 100,
    ) -> HistoricalExecutionSlice: ...


class CompletedBarPort(Protocol):
    def add_completed_bar_listener(
        self,
        frequency: str,
        listener: Callable[[CompletedBar], None],
    ) -> None: ...

    def remove_completed_bar_listener(
        self,
        frequency: str,
        listener: Callable[[CompletedBar], None],
    ) -> None: ...


class CalendarPort(Protocol):
    def is_trading_session(self, value: str) -> bool: ...
    def next_trading_session(self, value: str) -> str: ...


class InstrumentMetadataPort(Protocol):
    def get_instruments(self, instruments: Sequence[str]) -> dict[str, InstrumentMetadata]: ...


class AccountSnapshotPort(Protocol):
    def account_snapshot(self) -> AccountSnapshot: ...
    def quotes(self, instruments: Sequence[str]) -> dict[str, TradableQuote]: ...


class ExecutionRoutePort(Protocol):
    """Trusted execution boundary; providers never receive this port."""

    def submit_child(self, child: ExecutionChild) -> str | None: ...
    def child_statuses(self, references: Sequence[str]) -> dict[str, str]: ...
    def child_fills(self, references: Sequence[str]) -> list[dict[str, Any]]: ...
    def cancel_child(self, reference: str) -> bool: ...


class StrategyRuntimeFactoryPort(Protocol):
    def create_runtime(self, instance_id: str, runtime: Any) -> Any: ...


@dataclass(frozen=True)
class RuntimeCommandResult:
    ok: bool
    command_id: str = ""
    runtime_id: str = ""
    heartbeat_at: str = ""
    runner_status: dict[str, Any] = field(default_factory=dict)
    recovery: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    timed_out: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class RuntimeControlPort(Protocol):
    """Control a concrete daemon without exposing it to the application layer."""

    def status(self, instance: dict[str, Any]) -> RuntimeCommandResult: ...
    def start(self, instance: dict[str, Any]) -> RuntimeCommandResult: ...
    def pause(self, instance: dict[str, Any]) -> RuntimeCommandResult: ...
    def reconcile(self, instance: dict[str, Any]) -> RuntimeCommandResult: ...
    def resume(self, instance: dict[str, Any]) -> RuntimeCommandResult: ...
    def stop(self, instance: dict[str, Any]) -> RuntimeCommandResult: ...
