"""Focused compatibility regressions for the vendored AlphaForge runtime."""

from __future__ import annotations

import numpy as np
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
