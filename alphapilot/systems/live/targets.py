"""TargetPortfolio — the first-class hand-off from *decision* to *execution*.

The daily selection strategy decides a target book (which instruments to hold and
how many shares) and the timing strategy emits order intents; both are turned into
a broker-agnostic :class:`TargetPortfolio`, which the executor reconciles against
the **real** account. Decoupling "what to hold" from "how it filled in a
simulation" is exactly the interface change the plan calls for: the live executor
must diff against real positions from the OMS, never against a simulated roll.
"""

from __future__ import annotations

from typing import Any

from alphapilot.systems.trading.contracts import (
    AccountSnapshot,
    TargetPortfolio,
    TargetPosition,
)
from alphapilot.systems.live.types import Direction


def parse_target_positions(raw: Any) -> list[TargetPosition]:
    """Parse future-ready target positions from JSON-like input."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("positions must be a list")
    positions: list[TargetPosition] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("instrument") or "").strip()
        if not symbol:
            continue
        direction_raw = str(row.get("direction") or Direction.NET.value).lower()
        try:
            direction = Direction(direction_raw)
        except ValueError:
            direction = Direction.NET
        positions.append(
            TargetPosition(
                symbol=symbol,
                target_volume=float(row.get("target_volume") or row.get("volume") or 0.0),
                direction=direction,
                price=float(row.get("price") or 0.0),
                offset_policy=str(row.get("offset_policy") or "auto"),
            )
        )
    return positions
