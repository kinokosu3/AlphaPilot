from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from alphapilot.systems.research.gates import (
    calibrate_factor_direction,
    choose_development_champion,
    evaluate_economic_gate,
    evaluate_factor_gate,
    select_diverse_factors,
    validate_factor_expression,
)


def _panel(seed: int = 7) -> tuple[pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=300)
    instruments = [f"S{i:02d}" for i in range(12)]
    index = pd.MultiIndex.from_product(
        [dates, instruments], names=["datetime", "instrument"]
    )
    factor = pd.Series(rng.normal(size=len(index)), index=index, name="factor")
    label = 0.45 * factor + pd.Series(
        rng.normal(size=len(index)), index=index, name="noise"
    )
    return factor, label


def test_expression_gate_rejects_future_data_and_long_lookback() -> None:
    assert validate_factor_expression("$close/Ref($close,5)-1")["passed"] is True

    future = validate_factor_expression("Ref($close,-1)/$close-1")
    long_window = validate_factor_expression("Mean($close,121)/$close-1")

    assert future["passed"] is False
    assert "future reference" in future["errors"][0]
    assert long_window["passed"] is False
    assert long_window["max_lookback"] == 121


def test_factor_gate_uses_frozen_direction_and_development_thresholds() -> None:
    factor, label = _panel()
    direction = calibrate_factor_direction(factor.iloc[:2400], label.iloc[:2400])

    result = evaluate_factor_gate(
        factor,
        label,
        direction=direction,
        expression="Mean($close,20)/$close-1",
    )

    assert direction == 1
    assert result["passed"] is True
    assert result["finite_coverage"] == 1.0
    assert result["nonconstant_day_ratio"] == 1.0
    assert result["mean_rank_ic"] >= 0.015
    assert result["rank_icir"] >= 0.30
    assert result["month_direction_ratio"] >= 0.55


def test_diverse_selection_rejects_ast_duplicates_and_correlated_values() -> None:
    first, _ = _panel()
    rng = np.random.default_rng(99)
    independent = pd.Series(rng.normal(size=len(first)), index=first.index)
    candidates = [
        {
            "name": "first",
            "expression": "Mean($close,5)+Mean($volume,5)",
            "values": first,
            "score": 4.0,
            "source": "llm",
            "hypothesis": "h1",
        },
        {
            "name": "duplicate",
            "expression": "Mean($volume,5)+Mean($close,5)",
            "values": independent,
            "score": 3.0,
            "source": "llm",
            "hypothesis": "h1",
        },
        {
            "name": "correlated",
            "expression": "Std($close,7)",
            "values": first * 2,
            "score": 2.0,
            "source": "rl",
            "hypothesis": "rl",
        },
        {
            "name": "independent",
            "expression": "Std($volume,11)",
            "values": independent,
            "score": 1.0,
            "source": "rl",
            "hypothesis": "rl",
        },
    ]

    result = select_diverse_factors(candidates, max_factors=4)

    assert result["selected_names"] == ["first", "independent"]
    reasons = {item["name"]: item["reason"] for item in result["rejected"]}
    assert reasons == {"duplicate": "duplicate_ast", "correlated": "correlation"}


def test_economic_gate_checks_double_cost_rolling_windows_and_baseline() -> None:
    rng = np.random.default_rng(18)
    dates = pd.bdate_range("2025-01-02", periods=320)
    benchmark = rng.normal(0.0001, 0.004, len(dates))
    excess = rng.normal(0.00035, 0.001, len(dates))
    report = pd.DataFrame(
        {
            "net_return": benchmark + excess,
            "benchmark_return": benchmark,
            "cost": 0.00004,
            "turnover": 0.10,
        },
        index=dates,
    )

    result = evaluate_economic_gate(
        report,
        baseline_metrics={"annualized_excess": 0.03, "information_ratio": 0.5},
    )

    assert result["passed"] is True
    assert result["metrics"]["annualized_excess"] >= 0.03
    assert result["metrics"]["double_cost_total_return"] > 0
    assert result["metrics"]["double_cost_total_excess"] > 0

    report["turnover"] = 0.25
    failed = evaluate_economic_gate(
        report,
        baseline_metrics={"annualized_excess": 0.03, "information_ratio": 0.5},
    )
    assert failed["passed"] is False
    assert "average_daily_turnover" in failed["failures"]


def test_champion_tie_breaks_use_ir_then_factor_count_then_turnover() -> None:
    champion = choose_development_champion(
        [
            {
                "name": "many",
                "net_information_ratio": 1.2,
                "factor_count": 8,
                "average_daily_turnover": 0.10,
            },
            {
                "name": "few",
                "net_information_ratio": 1.2,
                "factor_count": 5,
                "average_daily_turnover": 0.15,
            },
            {
                "name": "failed",
                "passed": False,
                "net_information_ratio": 2.0,
                "factor_count": 1,
                "average_daily_turnover": 0.01,
            },
        ]
    )
    assert champion["name"] == "few"
    with pytest.raises(ValueError, match="no development candidate"):
        choose_development_champion([{"passed": False}])
