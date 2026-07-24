"""Mode-neutral execution-quality diagnostics for forward runs."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def evaluate_implementation_shortfall(
    fills: pd.DataFrame,
    *,
    max_median_bp: float = 20.0,
    max_p95_bp: float = 50.0,
) -> dict[str, Any]:
    """Aggregate partial fills by order and evaluate adverse shortfall in bp."""

    required = {"order_reference", "side", "arrival_price", "fill_price", "volume"}
    missing = sorted(required - set(fills.columns))
    if missing:
        raise ValueError(f"execution-quality data is missing: {', '.join(missing)}")
    frame = fills.copy()
    for column in ("arrival_price", "fill_price", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.replace([np.inf, -np.inf], np.nan).dropna(
        subset=["arrival_price", "fill_price", "volume"]
    )
    frame = frame[
        (frame["arrival_price"] > 0)
        & (frame["fill_price"] > 0)
        & (frame["volume"] > 0)
    ]
    if frame.empty:
        raise ValueError("execution-quality data has no valid fills")
    orders: list[dict[str, Any]] = []
    for reference, group in frame.groupby("order_reference", sort=True):
        sides = {str(value).strip().lower() for value in group["side"]}
        if len(sides) != 1 or not sides <= {"buy", "sell", "long", "short"}:
            raise ValueError(f"order {reference!r} has an invalid or mixed side")
        side = next(iter(sides))
        arrival_values = group["arrival_price"].dropna().unique()
        if len(arrival_values) != 1:
            raise ValueError(f"order {reference!r} has multiple arrival prices")
        arrival = float(arrival_values[0])
        volume = float(group["volume"].sum())
        average_fill = float(
            (group["fill_price"] * group["volume"]).sum() / volume
        )
        if side in {"buy", "long"}:
            shortfall_bp = (average_fill / arrival - 1.0) * 10_000.0
        else:
            shortfall_bp = (arrival / average_fill - 1.0) * 10_000.0
        orders.append(
            {
                "order_reference": str(reference),
                "side": side,
                "arrival_price": arrival,
                "average_fill_price": average_fill,
                "volume": volume,
                "shortfall_bp": shortfall_bp,
            }
        )
    values = np.asarray([row["shortfall_bp"] for row in orders], dtype=float)
    median = float(np.median(values))
    p95 = float(np.percentile(values, 95))
    checks = {
        "median_implementation_shortfall": math.isfinite(median)
        and median <= max_median_bp,
        "p95_implementation_shortfall": math.isfinite(p95) and p95 <= max_p95_bp,
    }
    return {
        "passed": all(checks.values()),
        "order_count": len(orders),
        "median_bp": median,
        "p95_bp": p95,
        "checks": checks,
        "failures": [name for name, passed in checks.items() if not passed],
        "orders": orders,
    }
