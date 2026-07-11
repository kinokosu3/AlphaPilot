"""Small, durable event helpers for the live-trading runtime.

The in-memory path still uses direct callbacks for speed and simplicity. These
helpers define the JSON shape we persist to the ledger/daemon journal so CLI,
Portal and recovery code can reason about the same event fields.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class LiveEvent:
    """A JSON-serializable live runtime event."""

    ts: str
    kind: str
    payload: Any = None
    command_id: str | None = None
    order_id: str | None = None
    reference: str | None = None
    source: str = "live"

    def to_dict(self) -> dict[str, Any]:
        data = {
            "ts": self.ts,
            "kind": self.kind,
            "payload": jsonable(self.payload),
            "source": self.source,
        }
        if self.command_id:
            data["command_id"] = self.command_id
        if self.order_id:
            data["order_id"] = self.order_id
        if self.reference:
            data["reference"] = self.reference
        return data


EventHandler = Callable[[LiveEvent], None]


class LiveEventBus:
    """Small in-process event bus for the live runtime.

    It is deliberately synchronous and dependency-free: gateway callbacks still
    flow through the engine immediately, while observers such as tests, Portal
    bridges or metrics collectors can subscribe without becoming part of the
    gateway/OMS call chain. Handler failures are isolated and recorded so one
    observer cannot break trading state updates.
    """

    def __init__(self, *, error_handler: Callable[[BaseException, LiveEvent], None] | None = None) -> None:
        self._handlers: dict[str | None, list[EventHandler]] = defaultdict(list)
        self._lock = RLock()
        self._error_handler = error_handler
        self.errors: list[dict[str, Any]] = []

    def subscribe(self, kind: str | None, handler: EventHandler) -> Callable[[], None]:
        """Subscribe ``handler`` to one kind, or all events when ``kind`` is ``None``."""
        key = None if kind in (None, "*") else str(kind)
        with self._lock:
            if handler not in self._handlers[key]:
                self._handlers[key].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                handlers = self._handlers.get(key, [])
                if handler in handlers:
                    handlers.remove(handler)
                if not handlers and key in self._handlers:
                    self._handlers.pop(key, None)

        return unsubscribe

    def publish(
        self,
        kind: str,
        payload: Any = None,
        *,
        command_id: str | None = None,
        order_id: str | None = None,
        reference: str | None = None,
        source: str = "live",
        now_fn=datetime.now,
    ) -> LiveEvent:
        """Create and publish one event."""
        event = make_event(
            kind,
            payload,
            command_id=command_id,
            order_id=order_id,
            reference=reference,
            source=source,
            now_fn=now_fn,
        )
        self.publish_event(event)
        return event

    def publish_event(self, event: LiveEvent) -> LiveEvent:
        """Publish an existing event object."""
        with self._lock:
            handlers = list(self._handlers.get(None, []))
            handlers.extend(self._handlers.get(event.kind, []))
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001 - observers must not break live trading
                self.errors.append({
                    "kind": event.kind,
                    "error": f"{type(exc).__name__}: {exc}",
                    "source": event.source,
                })
                if self._error_handler is not None:
                    try:
                        self._error_handler(exc, event)
                    except Exception as handler_exc:  # noqa: BLE001
                        self.errors.append({
                            "kind": event.kind,
                            "error": f"error_handler:{type(handler_exc).__name__}: {handler_exc}",
                            "source": event.source,
                        })
        return event


def make_event(
    kind: str,
    payload: Any = None,
    *,
    command_id: str | None = None,
    order_id: str | None = None,
    reference: str | None = None,
    source: str = "live",
    now_fn=datetime.now,
) -> LiveEvent:
    """Build a timestamped live event."""
    return LiveEvent(
        ts=now_fn().isoformat(timespec="seconds"),
        kind=str(kind),
        payload=jsonable(payload),
        command_id=command_id,
        order_id=order_id,
        reference=reference,
        source=source,
    )


def jsonable(value: Any) -> Any:
    """Best-effort conversion for dataclasses, enums and paths."""
    if is_dataclass(value) and not isinstance(value, type):
        return {key: jsonable(val) for key, val in asdict(value).items()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if hasattr(value, "value"):
        return value.value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)
