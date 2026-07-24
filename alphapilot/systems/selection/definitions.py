"""Selection-system contributions to the shared strategy registry."""

from __future__ import annotations

import hashlib
from pathlib import Path

from alphapilot.systems.trading.contracts import SignalKind
from alphapilot.systems.trading.domain import StrategyDefinition


def strategy_definitions() -> list[StrategyDefinition]:
    source = Path(__file__).with_name("qlib_provider.py")
    inference = Path(__file__).with_name("predict.py")
    digest = hashlib.sha256(source.read_bytes())
    digest.update(inference.read_bytes())
    return [
        StrategyDefinition(
            strategy_id="qlib_selection",
            version="1.0.0",
            kind="model_selection",
            factory="alphapilot.systems.selection.qlib_provider:QlibSelectionProvider",
            parameter_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            supported_assets=("equity", "fund"),
            supported_frequencies=("day",),
            output_type="cross_sectional_scores",
            required_history=1,
            state_schema_version=1,
            source="builtin",
            code_hash=digest.hexdigest(),
            description="Qlib model scores over a point-in-time equity universe.",
            api_version=2,
            provider_api_version=2,
            signal_kind=SignalKind.CROSS_SECTIONAL_SELECTION,
            supported_run_modes=("paper", "simulation", "shadow", "live"),
        )
    ]
