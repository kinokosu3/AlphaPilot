from __future__ import annotations

import numpy as np
import pandas as pd

from alphapilot.systems.research.selection import (
    RESEARCH_PORTFOLIO_POLICY,
    preregister_candidate_sets,
    validate_development_evidence,
)


def _factor(name: str, source: str, hypothesis: str, seed: int) -> dict:
    rng = np.random.default_rng(seed)
    index = pd.MultiIndex.from_product(
        [pd.bdate_range("2023-01-02", periods=20), ["A", "B", "C", "D"]],
        names=["datetime", "instrument"],
    )
    return {
        "name": name,
        "source": source,
        "hypothesis": hypothesis,
        "expression": f"Mean($close,{seed + 2})/Mean($close,{seed + 3})",
        "values": pd.Series(rng.normal(size=len(index)), index=index),
        "score": 1 / seed,
        "gate": {"passed": True, "mean_rank_ic": 0.02},
    }


def test_preregistration_applies_hypothesis_and_source_caps() -> None:
    factors = [
        *[_factor(f"llm_h1_{i}", "llm_mining", "h1", i + 1) for i in range(3)],
        *[_factor(f"llm_h2_{i}", "llm_mining", "h2", i + 10) for i in range(3)],
        *[_factor(f"llm_h3_{i}", "llm_mining", "h3", i + 20) for i in range(3)],
        *[_factor(f"rl_{i}", "alphaforge_rl", "rl", i + 30) for i in range(8)],
    ]

    result = preregister_candidate_sets(
        baseline_factor_names=["b1", "b2", "b3", "b4"],
        qualified_factors=factors,
    )

    candidates = result["candidates"]
    assert len(candidates["llm_combination"]["factor_names"]) == 6
    assert len(candidates["rl_combination"]["factor_names"]) == 6
    assert len(candidates["mixed_combination"]["factor_names"]) == 10
    selected_llm = set(candidates["llm_combination"]["factor_names"])
    assert sum(name.startswith("llm_h1") for name in selected_llm) <= 2


def test_development_evidence_requires_ablations_and_selects_one_champion() -> None:
    registration = {
        "candidates": {
            "system_baseline": {
                "factor_names": ["b1", "b2", "b3", "b4"],
                "available": True,
            },
            "llm_combination": {
                "factor_names": ["l1", "l2"],
                "available": True,
            },
            "rl_combination": {"factor_names": [], "available": False},
            "mixed_combination": {"factor_names": [], "available": False},
        }
    }

    def evidence(name: str, factors: list[str], ir: float) -> dict:
        return {
            "name": name,
            "factor_names": factors,
            "portfolio_policy": RESEARCH_PORTFOLIO_POLICY,
            "qlib_template_fingerprint": "template",
            "single_factor_results": {factor: {"passed": True} for factor in factors},
            "leave_one_out_results": {factor: {"passed": True} for factor in factors},
            "combination_result": {
                "passed": True,
                "net_information_ratio": ir,
                "average_daily_turnover": 0.1,
            },
        }

    result = validate_development_evidence(
        registration,
        [
            evidence("system_baseline", ["b1", "b2", "b3", "b4"], 0.7),
            evidence("llm_combination", ["l1", "l2"], 0.8),
        ],
        qlib_template_fingerprint="template",
    )

    assert result["passed"] is True
    assert result["champion_name"] == "llm_combination"
    assert result["blind_test_candidates"] == ["system_baseline", "llm_combination"]

    broken = evidence("llm_combination", ["l1", "l2"], 0.8)
    broken["leave_one_out_results"].pop("l2")
    failed = validate_development_evidence(
        registration,
        [
            evidence("system_baseline", ["b1", "b2", "b3", "b4"], 0.7),
            broken,
        ],
        qlib_template_fingerprint="template",
    )
    assert failed["passed"] is False
    assert any("leave-one-out" in item for item in failed["failures"])
