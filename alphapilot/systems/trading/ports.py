"""Application ports shared with concrete live-runtime adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol


class RouteOrigin(str, Enum):
    MANUAL = "manual"
    AUTOMATED = "automated"


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

