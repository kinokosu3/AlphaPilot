"""Timing-system contributions to the shared strategy registry."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
from typing import Any

from alphapilot.systems.trading.contracts import SignalKind
from alphapilot.systems.trading.domain import StrategyDefinition
from alphapilot.systems.timing.strategies import _STRATEGY_CLASSES


def strategy_definitions() -> list[StrategyDefinition]:
    return [
        StrategyDefinition(
            strategy_id=cls.name,
            version="1.0.0",
            kind="rule",
            factory=cls,
            parameter_schema=_schema_from_defaults(cls.defaults),
            required_history=_required_history(cls.defaults),
            source="builtin",
            code_hash=_source_hash(cls),
            description=cls.description,
            api_version=1,
            provider_api_version=1,
            signal_kind=SignalKind.INSTRUMENT_TIMING,
            supported_run_modes=("paper", "simulation", "shadow", "live"),
        )
        for cls in _STRATEGY_CLASSES
    ]


def _schema_from_defaults(defaults: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for key, value in defaults.items():
        # Exposure belongs to the portfolio policy in the formal runtime.  The
        # legacy timing adapter still accepts target_percent as an alias.
        if key == "target_percent":
            continue
        if isinstance(value, bool):
            spec = {"type": "boolean", "default": value}
        elif isinstance(value, int):
            spec = {"type": "integer", "default": value}
        elif isinstance(value, float):
            spec = {"type": "number", "default": value}
        else:
            spec = {"type": "string", "default": value}
        if "window" in key:
            spec["minimum"] = 1
        if key == "target_percent":
            spec.update({"minimum": 0.0, "maximum": 1.0})
        properties[key] = spec
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _required_history(defaults: dict[str, Any]) -> int:
    values = [
        int(value) for key, value in defaults.items()
        if "window" in key and isinstance(value, int)
    ]
    return max(values or [1]) + 1


def _source_hash(value: Any) -> str:
    source = inspect.getsourcefile(value)
    return hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else ""
