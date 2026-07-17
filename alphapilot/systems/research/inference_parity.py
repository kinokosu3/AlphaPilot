"""Deterministic offline-versus-formal inference snapshots.

The deployment parity service compares persisted decisions.  This module adds
the earlier research boundary: factor values and scores produced before a
decision exists must also be identical under the frozen data context.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

import pandas as pd


def _hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _number(value: Any) -> str:
    """Represent a scalar without locale or JSON float-rounding ambiguity."""

    if value is None or pd.isna(value):
        return "nan"
    number = float(value)
    if math.isinf(number):
        return "+inf" if number > 0 else "-inf"
    if number == 0:
        number = 0.0
    return number.hex()


def _instrument(value: Any) -> str:
    return str(value).strip().upper()


def _factor_rows(factors: pd.DataFrame) -> list[dict[str, Any]]:
    if not isinstance(factors, pd.DataFrame):
        raise TypeError("factors must be a pandas DataFrame")
    if factors.columns.duplicated().any():
        raise ValueError("factor columns must be unique")
    frame = factors.copy()
    if isinstance(frame.index, pd.MultiIndex):
        names = list(frame.index.names)
        instrument_level: str | int = (
            "instrument" if "instrument" in names else frame.index.nlevels - 1
        )
        instruments = frame.index.get_level_values(instrument_level)
    else:
        instruments = frame.index
    if pd.Index(instruments).duplicated().any():
        raise ValueError("factor snapshot contains duplicate instruments")
    rows = []
    for position, instrument in enumerate(instruments):
        row = frame.iloc[position]
        rows.append(
            {
                "instrument": _instrument(instrument),
                "values": {
                    str(column): _number(row[column])
                    for column in sorted(frame.columns, key=str)
                },
            }
        )
    return sorted(rows, key=lambda item: item["instrument"])


def _values(value: pd.Series | Mapping[str, Any], *, name: str) -> dict[str, str]:
    items = value.items() if isinstance(value, (pd.Series, Mapping)) else None
    if items is None:
        raise TypeError(f"{name} must be a pandas Series or mapping")
    normalized: dict[str, str] = {}
    for instrument, scalar in items:
        symbol = _instrument(instrument)
        if symbol in normalized:
            raise ValueError(f"{name} contains duplicate instrument {symbol}")
        encoded = _number(scalar)
        if encoded in {"nan", "+inf", "-inf"}:
            raise ValueError(f"{name} must contain only finite values")
        normalized[symbol] = encoded
    return dict(sorted(normalized.items()))


def factor_values_hash(factors: pd.DataFrame) -> str:
    """Hash one date's complete cross-sectional factor matrix."""

    return _hash(_factor_rows(factors))


def numeric_mapping_hash(
    value: pd.Series | Mapping[str, Any],
    *,
    name: str = "values",
) -> str:
    """Hash finite instrument-keyed scores or weights."""

    return _hash(_values(value, name=name))


def build_inference_snapshot(
    *,
    as_of: str,
    provider_uri: str,
    market: str,
    factor_data_fingerprint: str,
    factors: pd.DataFrame,
    scores: pd.Series | Mapping[str, Any],
    target_weights: pd.Series | Mapping[str, Any],
    whitelist: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build a canonical, hash-addressed inference snapshot for one session."""

    session = str(pd.Timestamp(as_of).date())
    context = {
        "provider_uri": str(provider_uri),
        "market": str(market),
        "factor_data_fingerprint": str(factor_data_fingerprint),
    }
    if not all(context.values()):
        raise ValueError("provider_uri, market and factor_data_fingerprint are required")
    factor_rows = _factor_rows(factors)
    score_values = _values(scores, name="scores")
    weight_values = _values(target_weights, name="target_weights")
    whitelist_values = sorted({_instrument(item) for item in (whitelist or ())})
    if whitelist_values:
        outside = sorted(set(score_values) - set(whitelist_values))
        if outside:
            raise ValueError(
                "formal scores must be filtered to the frozen whitelist; "
                f"outside instruments: {outside}"
            )
    ranking = sorted(
        score_values,
        key=lambda symbol: (-float.fromhex(score_values[symbol]), symbol),
    )
    components = {
        "context": context,
        "factor_values": factor_rows,
        "scores": score_values,
        "ranking": ranking,
        "target_weights": weight_values,
        "whitelist": whitelist_values,
    }
    hashes = {name: _hash(value) for name, value in components.items()}
    snapshot = {
        "schema_version": 1,
        "as_of": session,
        **components,
        "hashes": hashes,
    }
    snapshot["snapshot_hash"] = _hash(snapshot)
    return snapshot


def compare_inference_snapshots(
    offline: Mapping[str, Any],
    formal: Mapping[str, Any],
) -> dict[str, Any]:
    """Require exact same-session context, factors, scores, ranking and weights."""

    fields = (
        "as_of",
        "context",
        "factor_values",
        "scores",
        "ranking",
        "target_weights",
        "whitelist",
    )
    differences = {
        field: {
            "offline_hash": _hash(offline.get(field)),
            "formal_hash": _hash(formal.get(field)),
        }
        for field in fields
        if offline.get(field) != formal.get(field)
    }
    return {
        "passed": not differences,
        "as_of": str(offline.get("as_of") or formal.get("as_of") or ""),
        "offline_snapshot_hash": str(offline.get("snapshot_hash") or ""),
        "formal_snapshot_hash": str(formal.get("snapshot_hash") or ""),
        "differences": differences,
    }
