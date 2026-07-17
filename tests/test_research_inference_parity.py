from __future__ import annotations

import pandas as pd

from alphapilot.systems.research.inference_parity import (
    build_inference_snapshot,
    compare_inference_snapshots,
)


def _snapshot(*, provider: str = "/pit/a", score: float = 0.8):
    factors = pd.DataFrame(
        {"reversal": [0.1, 0.2], "trend": [1.0, -1.0]},
        index=pd.Index(["SH600000", "SZ000001"], name="instrument"),
    )
    return build_inference_snapshot(
        as_of="2026-07-16T15:00:00+08:00",
        provider_uri=provider,
        market="main_stock_pit",
        factor_data_fingerprint="pit-fingerprint",
        factors=factors,
        scores={"SH600000": score, "SZ000001": 0.2},
        target_weights={"SH600000": 0.02, "SZ000001": 0.02},
        whitelist=["SH600000", "SZ000001"],
    )


def test_same_date_offline_and_formal_inference_are_exactly_reproducible() -> None:
    offline = _snapshot()
    formal = _snapshot()

    result = compare_inference_snapshots(offline, formal)

    assert result["passed"] is True
    assert result["differences"] == {}
    assert offline["hashes"]["factor_values"] == formal["hashes"]["factor_values"]
    assert offline["ranking"] == ["SH600000", "SZ000001"]


def test_context_or_score_cross_use_fails_closed() -> None:
    offline = _snapshot()
    wrong_context = _snapshot(provider="/pit/b")
    wrong_score = _snapshot(score=0.1)

    context_result = compare_inference_snapshots(offline, wrong_context)
    score_result = compare_inference_snapshots(offline, wrong_score)

    assert context_result["passed"] is False
    assert "context" in context_result["differences"]
    assert score_result["passed"] is False
    assert {"scores", "ranking"} <= set(score_result["differences"])


def test_scores_must_already_be_filtered_to_frozen_whitelist() -> None:
    factors = pd.DataFrame(
        {"factor": [1.0]},
        index=pd.Index(["SH600000"], name="instrument"),
    )
    try:
        build_inference_snapshot(
            as_of="2026-07-16",
            provider_uri="/pit/a",
            market="main_stock_pit",
            factor_data_fingerprint="fingerprint",
            factors=factors,
            scores={"SH600000": 1.0, "SZ000001": 0.5},
            target_weights={"SH600000": 0.02},
            whitelist=["SH600000"],
        )
    except ValueError as exc:
        assert "frozen whitelist" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("out-of-whitelist score was accepted")
