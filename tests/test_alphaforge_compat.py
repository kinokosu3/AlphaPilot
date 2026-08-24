"""Focused compatibility regressions for the vendored AlphaForge runtime."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import torch

import alphapilot.modules.alphaforge  # noqa: F401 - installs vendored import paths


def test_lstm_generator_forward_uses_numpy2_compatible_token_dtype(
    monkeypatch,
) -> None:
    """Exercise the autoregressive NumPy -> Torch token conversion on CPU."""
    from gan.network import generater

    class _Builders:
        def __init__(self, batch_size: int) -> None:
            self.batch_size = batch_size
            self.tokens: list[np.ndarray] = []

        def get_valid_op(self) -> np.ndarray:
            return np.ones((self.batch_size, 4), dtype=bool)

        def add_token(self, token: np.ndarray) -> None:
            self.tokens.append(np.asarray(token))

    monkeypatch.setattr(generater, "Builders", _Builders)
    model = generater.NetG_Lstm(
        n_chars=4,
        n_layers=1,
        d_model=4,
        dropout=0.0,
        seq_len=2,
        potential_size=3,
    )

    (tokens, logits, masks), builders = model(torch.zeros((2, 3)))

    assert tokens.shape == (2, 2)
    assert logits.shape == (2, 2, 4)
    assert masks.shape == (2, 2, 4)
    assert len(builders.tokens) == 2


def test_calendar_range_clamps_to_full_padding() -> None:
    from alphagen_qlib.stock_data import _resolve_padded_calendar_range

    calendar = np.asarray(pd.date_range("2017-01-01", periods=300, freq="D"))

    start, end = _resolve_padded_calendar_range(
        calendar,
        "2010-01-01",
        "2030-01-01",
        max_backtrack_days=100,
        max_future_days=30,
    )

    assert start == 100
    assert end == 269


def test_calendar_range_resolves_non_trading_date_boundaries() -> None:
    from alphagen_qlib.stock_data import _resolve_padded_calendar_range

    calendar = np.asarray(pd.bdate_range("2024-01-01", periods=10))

    start, end = _resolve_padded_calendar_range(
        calendar,
        "2024-01-06",  # Saturday -> next trading session
        "2024-01-09",
        max_backtrack_days=2,
        max_future_days=1,
    )

    assert pd.Timestamp(calendar[start]) == pd.Timestamp("2024-01-08")
    assert pd.Timestamp(calendar[end]) == pd.Timestamp("2024-01-09")


@pytest.mark.parametrize(
    ("start_time", "end_time"),
    [
        ("2010-01-01", "2010-12-31"),
        ("2030-01-01", "2030-12-31"),
    ],
)
def test_calendar_range_rejects_intervals_outside_the_provider(
    start_time: str,
    end_time: str,
) -> None:
    from alphagen_qlib.stock_data import _resolve_padded_calendar_range

    calendar = np.asarray(pd.date_range("2017-01-01", periods=120, freq="D"))

    with pytest.raises(ValueError, match="No usable Qlib dates"):
        _resolve_padded_calendar_range(
            calendar,
            start_time,
            end_time,
            max_backtrack_days=20,
            max_future_days=10,
        )


@pytest.mark.parametrize(
    ("max_backtrack_days", "max_future_days"),
    [(-1, 0), (0, -1), (True, 0)],
)
def test_calendar_range_rejects_invalid_padding(
    max_backtrack_days: int,
    max_future_days: int,
) -> None:
    from alphagen_qlib.stock_data import _resolve_padded_calendar_range

    calendar = np.asarray(pd.date_range("2017-01-01", periods=120, freq="D"))

    with pytest.raises(ValueError, match="non-negative integer"):
        _resolve_padded_calendar_range(
            calendar,
            "2017-02-01",
            "2017-03-01",
            max_backtrack_days=max_backtrack_days,
            max_future_days=max_future_days,
        )
