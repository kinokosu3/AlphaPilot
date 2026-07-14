"""Outer data adapters for the shared strategy runtime contracts."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from typing import Any, Sequence

import pandas as pd

from alphapilot.systems.trading.contracts import (
    CompletedBar,
    InstrumentMetadata,
    PriceAdjustment,
    TradableQuote,
    canonical_instrument,
)


class TimingHistoricalDataAdapter:
    """Expose existing local CSV storage through ``HistoricalDataPort``."""

    def __init__(self, timing_system: Any, *, data_dir: str | None = None) -> None:
        self.timing_system = timing_system
        self.data_dir = data_dir

    def load_completed_bars(
        self,
        *,
        instruments: Sequence[str],
        start: str | None,
        end: str | None,
        frequency: str,
        adjustment: str,
    ) -> list[CompletedBar]:
        mode = PriceAdjustment(str(adjustment))
        frame = self.timing_system.load_bars(
            symbols=list(instruments),
            start_date=start,
            end_date=end,
            freq=frequency,
            data_dir=self.data_dir,
            adjust_mode=mode.value,
        )
        return completed_bars_from_frame(
            frame,
            frequency=frequency,
            adjustment=mode,
        )


def completed_bars_from_frame(
    frame: pd.DataFrame,
    *,
    frequency: str,
    adjustment: PriceAdjustment | str,
    data_version: str = "",
) -> list[CompletedBar]:
    mode = adjustment if isinstance(adjustment, PriceAdjustment) else PriceAdjustment(str(adjustment))
    required = {"datetime", "instrument", "open", "high", "low", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"completed-bar frame is missing columns: {missing}")
    ordered = frame.sort_values(["datetime", "instrument"]).copy()
    if ordered.duplicated(["datetime", "instrument"]).any():
        raise ValueError("completed-bar frame contains duplicate instrument timestamps")
    version = data_version or _frame_version(ordered, frequency=frequency, adjustment=mode.value)
    bars: list[CompletedBar] = []
    for row in ordered.itertuples(index=False):
        timestamp = pd.Timestamp(getattr(row, "datetime"))
        bars.append(CompletedBar(
            datetime=timestamp.isoformat(),
            instrument=canonical_instrument(str(getattr(row, "instrument"))),
            open=float(getattr(row, "open")),
            high=float(getattr(row, "high")),
            low=float(getattr(row, "low")),
            close=float(getattr(row, "close")),
            volume=float(getattr(row, "volume", 0.0) or 0.0),
            amount=float(getattr(row, "amount", 0.0) or 0.0),
            frequency=frequency,
            adjustment=mode,
            data_version=version,
            complete=True,
        ))
    return bars


def tradable_quotes_from_frame(
    frame: pd.DataFrame,
    *,
    frequency: str,
    data_version: str = "",
) -> dict[str, dict[str, TradableQuote]]:
    """Preserve optional historical tradability flags outside feature bars."""

    required = {"datetime", "instrument", "open", "close"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"tradable-quote frame is missing columns: {missing}")
    if frame.duplicated(["datetime", "instrument"]).any():
        raise ValueError("tradable-quote frame contains duplicate instrument timestamps")
    result: dict[str, dict[str, TradableQuote]] = {}
    for row in frame.itertuples(index=False):
        instant = pd.Timestamp(getattr(row, "datetime")).isoformat()
        session = instant[:10] if frequency == "day" else instant
        instrument = canonical_instrument(str(getattr(row, "instrument")))
        result.setdefault(session, {})[instrument] = TradableQuote(
            instrument=instrument,
            as_of=instant,
            last=float(getattr(row, "close")),
            open=float(getattr(row, "open")),
            limit_up=_optional_float(getattr(row, "limit_up", 0.0)),
            limit_down=_optional_float(getattr(row, "limit_down", 0.0)),
            suspended=_optional_bool(getattr(row, "suspended", False)),
            stale=_optional_bool(getattr(row, "stale", False)),
            data_version=data_version,
            price_source="raw",
        )
    return result


def instrument_metadata_from_frame(
    frame: pd.DataFrame,
    *,
    default_lot_size: int = 100,
) -> dict[str, InstrumentMetadata]:
    """Extract stable contract metadata, rejecting changes within one replay."""

    if "instrument" not in frame.columns:
        raise ValueError("instrument metadata frame is missing instrument")
    result: dict[str, InstrumentMetadata] = {}
    fields = ("asset_type", "lot_size", "price_tick", "settlement_days")
    for raw_instrument, group in frame.groupby("instrument", sort=True):
        values: dict[str, Any] = {}
        for field_name in fields:
            if field_name not in group.columns:
                continue
            unique = [value for value in group[field_name].dropna().unique().tolist()]
            if len(unique) > 1:
                raise ValueError(
                    f"instrument metadata {field_name} changes within replay for {raw_instrument}"
                )
            if unique:
                values[field_name] = unique[0]
        instrument = canonical_instrument(str(raw_instrument))
        asset_type = str(values.get("asset_type") or "equity").lower()
        result[instrument] = InstrumentMetadata(
            instrument=instrument,
            asset_type=asset_type,
            lot_size=int(values.get("lot_size") or default_lot_size),
            price_tick=float(values.get("price_tick") or 0.01),
            settlement_days=max(int(values.get("settlement_days", 1)), 0),
            long_only=asset_type in {"equity", "fund", "stock", "etf"},
        )
    return result


class SequenceCalendar:
    """Finite, explicit calendar used by preview/replay.

    Date-only values define trading sessions.  Timestamp values additionally
    define the exact completed-bar clock used by minute strategies.  No
    synthetic minute, lunch-break or overnight timestamp is invented.
    """

    def __init__(self, sessions: Sequence[str]) -> None:
        values = tuple(sorted({_timestamp(value) for value in sessions}))
        self.instants = values
        self.sessions = tuple(sorted({value[:10] for value in values}))
        self._index = {value: index for index, value in enumerate(self.sessions)}

    def is_trading_session(self, value: str) -> bool:
        return str(value)[:10] in self._index

    def next_trading_session(self, value: str) -> str:
        key = str(value)[:10]
        if key not in self._index:
            raise ValueError(f"{key} is absent from the explicit trading calendar")
        index = self._index[key] + 1
        if index >= len(self.sessions):
            raise ValueError(f"no next trading session is available after {key}")
        return self.sessions[index]

    def next_effective(self, value: str, frequency: str) -> str:
        if str(frequency) == "day":
            return self.next_trading_session(value)
        key = _timestamp(value)
        index = bisect_right(self.instants, key)
        if index >= len(self.instants):
            raise ValueError(f"no next completed-bar timestamp is available after {key}")
        return self.instants[index]

    def valid_until(self, effective: str, frequency: str) -> str:
        if str(frequency) == "day":
            return f"{str(effective)[:10]}T15:00:00+08:00"
        key = _timestamp(effective)
        index = bisect_right(self.instants, key)
        # A minute decision remains valid through its effective bar window,
        # but never past the following observed completed-bar timestamp.
        return self.instants[index] if index < len(self.instants) else key


class QlibCalendarAdapter:
    """Production calendar backed by the configured Qlib trading calendar."""

    def __init__(self, *, start: str = "2000-01-01", end: str = "2099-12-31") -> None:
        from qlib.data import D

        values = D.calendar(start_time=start, end_time=end, freq="day", future=True)
        sessions = [pd.Timestamp(value).date().isoformat() for value in values]
        if not sessions:
            raise RuntimeError("configured Qlib calendar is empty")
        self._delegate = SequenceCalendar(sessions)

    def is_trading_session(self, value: str) -> bool:
        return self._delegate.is_trading_session(value)

    def next_trading_session(self, value: str) -> str:
        return self._delegate.next_trading_session(value)


def _frame_version(frame: pd.DataFrame, *, frequency: str, adjustment: str) -> str:
    columns = [
        column for column in
        ("datetime", "instrument", "open", "high", "low", "close", "volume", "amount")
        if column in frame.columns
    ]
    digest = hashlib.sha256(
        json.dumps(
            {"frequency": frequency, "adjustment": adjustment, "columns": columns},
            sort_keys=True,
        ).encode("utf-8")
    )
    digest.update(pd.util.hash_pandas_object(frame[columns], index=False).values.tobytes())
    return digest.hexdigest()


def _timestamp(value: Any) -> str:
    raw = str(value)
    if len(raw) <= 10:
        return raw[:10]
    return pd.Timestamp(value).isoformat()


def _optional_float(value: Any) -> float:
    if value is None or pd.isna(value):
        return 0.0
    return float(value)


def _optional_bool(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)
