"""TargetPortfolio — the first-class hand-off from *decision* to *execution*.

The daily selection strategy decides a target book (which instruments to hold and
how many shares) and the timing strategy emits order intents; both are turned into
a broker-agnostic :class:`TargetPortfolio`, which the executor reconciles against
the **real** account. Decoupling "what to hold" from "how it filled in a
simulation" is exactly the interface change the plan calls for: the live executor
must diff against real positions from the OMS, never against a simulated roll.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from alphapilot.systems.live.types import Direction


@dataclass
class TargetPosition:
    """Future-ready target position expression.

    The current executor does not route these in LIVE mode yet; they are carried
    through parsing/status so future futures adapters can reuse the same target
    envelope.
    """

    symbol: str
    target_volume: float
    direction: Direction = Direction.NET
    price: float = 0.0
    offset_policy: str = "auto"


@dataclass
class TargetPortfolio:
    """A desired end-of-run holding book.

    ``holdings`` maps instrument code (any accepted symbol form) -> target shares.
    ``prices`` are reference/limit prices per code (for limit orders & valuation).
    """

    date: str
    holdings: dict[str, float] = field(default_factory=dict)
    prices: dict[str, float] = field(default_factory=dict)
    cash: float | None = None
    source: str = ""
    market: str | None = None
    positions: list[TargetPosition] = field(default_factory=list)

    @classmethod
    def from_holdings(
        cls,
        date: str,
        records: Iterable[dict[str, Any]],
        *,
        source: str = "",
        market: str | None = None,
    ) -> "TargetPortfolio":
        """Build from ``[{instrument, amount, price}, ...]`` (e.g. daily_trade holdings)."""
        holdings: dict[str, float] = {}
        prices: dict[str, float] = {}
        for row in records:
            code = row.get("instrument") or row.get("code")
            if code is None:
                continue
            amount = float(row.get("amount", 0) or 0)
            if amount <= 0:
                continue
            holdings[str(code)] = amount
            px = row.get("price")
            if px is not None:
                try:
                    prices[str(code)] = float(px)
                except (TypeError, ValueError):
                    pass
        return cls(date=date, holdings=holdings, prices=prices, source=source, market=market)


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
