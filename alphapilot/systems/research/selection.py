"""Preregister candidate sets and validate development-period evidence."""

from __future__ import annotations

from typing import Any, Iterable

from alphapilot.systems.research.gates import select_diverse_factors


RESEARCH_PORTFOLIO_POLICY = {
    "topk": 15,
    "n_drop": 3,
    "cash_buffer": 0.1,
    "max_position_weight": 0.1,
    "max_average_daily_turnover": 0.2,
}


def _source(value: Any) -> str:
    source = str(value or "").lower()
    if "llm" in source:
        return "llm"
    if source == "rl" or "alphaforge_rl" in source:
        return "rl"
    return source


def _qualified(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in items:
        gate = item.get("gate") if isinstance(item.get("gate"), dict) else item
        if gate.get("passed") is not True:
            continue
        candidate = dict(item)
        candidate["source"] = _source(candidate.get("source"))
        candidate["score"] = float(
            candidate.get("score", abs(float(gate.get("mean_rank_ic") or 0.0)))
        )
        result.append(candidate)
    return result


def preregister_candidate_sets(
    *,
    baseline_factor_names: list[str] | tuple[str, ...],
    qualified_factors: Iterable[dict[str, Any]],
    max_abs_spearman: float = 0.75,
) -> dict[str, Any]:
    """Build the four frozen candidate sets without forcing source representation."""

    baseline = list(dict.fromkeys(str(item) for item in baseline_factor_names if item))
    if len(baseline) != 4:
        raise ValueError("the system baseline must contain exactly four factors")
    factors = _qualified(qualified_factors)
    llm = [item for item in factors if item["source"] == "llm"]
    rl = [item for item in factors if item["source"] == "rl"]
    llm_result = select_diverse_factors(
        llm,
        max_factors=6,
        max_abs_spearman=max_abs_spearman,
        hypothesis_limit=2,
    ) if llm else {"selected_names": [], "rejected": [], "correlations": {}}
    rl_result = select_diverse_factors(
        rl,
        max_factors=6,
        max_abs_spearman=max_abs_spearman,
    ) if rl else {"selected_names": [], "rejected": [], "correlations": {}}
    mixed_result = select_diverse_factors(
        [*llm, *rl],
        max_factors=10,
        max_abs_spearman=max_abs_spearman,
    ) if llm or rl else {"selected_names": [], "rejected": [], "correlations": {}}

    candidates = {
        "system_baseline": {
            "factor_names": baseline,
            "available": True,
            "source": "baseline",
        },
        "llm_combination": {
            "factor_names": llm_result["selected_names"],
            "available": bool(llm_result["selected_names"]),
            "source": "llm",
        },
        "rl_combination": {
            "factor_names": rl_result["selected_names"],
            "available": bool(rl_result["selected_names"]),
            "source": "rl",
        },
        "mixed_combination": {
            "factor_names": mixed_result["selected_names"],
            "available": bool(mixed_result["selected_names"]),
            "source": "mixed",
        },
    }
    return {
        "candidates": candidates,
        "selection_details": {
            "llm": llm_result,
            "rl": rl_result,
            "mixed": mixed_result,
        },
        "portfolio_policy": dict(RESEARCH_PORTFOLIO_POLICY),
    }


def validate_development_evidence(
    preregistration: dict[str, Any],
    evidence: Iterable[dict[str, Any]],
    *,
    qlib_template_fingerprint: str,
) -> dict[str, Any]:
    """Require single/composite/leave-one-out results and choose one champion."""

    expected = dict(preregistration.get("candidates") or {})
    rows = {str(item.get("name") or ""): dict(item) for item in evidence}
    available = {name: item for name, item in expected.items() if item.get("available")}
    missing = sorted(set(available) - set(rows))
    unexpected = sorted(set(rows) - set(available))
    failures: list[str] = []
    if missing:
        failures.append(f"missing candidate evidence: {missing}")
    if unexpected:
        failures.append(f"unexpected/tuned candidates: {unexpected}")
    eligible: list[dict[str, Any]] = []
    for name, registration in available.items():
        row = rows.get(name)
        if row is None:
            continue
        factor_names = list(registration.get("factor_names") or [])
        if list(row.get("factor_names") or []) != factor_names:
            failures.append(f"{name}: factor list differs from preregistration")
        if row.get("portfolio_policy") != RESEARCH_PORTFOLIO_POLICY:
            failures.append(f"{name}: research portfolio policy changed")
        if str(row.get("qlib_template_fingerprint") or "") != str(
            qlib_template_fingerprint
        ):
            failures.append(f"{name}: Qlib/model template fingerprint changed")
        singles = dict(row.get("single_factor_results") or {})
        if set(singles) != set(factor_names):
            failures.append(f"{name}: single-factor evidence is incomplete")
        ablations = dict(row.get("leave_one_out_results") or {})
        if set(ablations) != set(factor_names):
            failures.append(f"{name}: leave-one-out evidence is incomplete")
        combination = dict(row.get("combination_result") or {})
        required_metrics = {
            "passed",
            "net_information_ratio",
            "average_daily_turnover",
        }
        if not required_metrics <= set(combination):
            failures.append(f"{name}: combination metrics are incomplete")
            continue
        if float(combination["average_daily_turnover"]) > 0.20:
            failures.append(f"{name}: turnover exceeds 20%")
        if combination.get("passed") is True:
            eligible.append(
                {
                    "name": name,
                    "factor_names": factor_names,
                    "factor_count": len(factor_names),
                    "net_information_ratio": float(
                        combination["net_information_ratio"]
                    ),
                    "average_daily_turnover": float(
                        combination["average_daily_turnover"]
                    ),
                }
            )
    if failures:
        return {"passed": False, "failures": failures, "champion": None}
    if not eligible:
        return {
            "passed": False,
            "failures": ["no preregistered development candidate passed"],
            "champion": None,
        }
    champion = min(
        eligible,
        key=lambda item: (
            -item["net_information_ratio"],
            item["factor_count"],
            item["average_daily_turnover"],
            item["name"],
        ),
    )
    return {
        "passed": True,
        "failures": [],
        "champion": champion,
        "champion_name": champion["name"],
        "champion_factor_names": champion["factor_names"],
        "eligible_candidates": eligible,
        "blind_test_candidates": ["system_baseline", champion["name"]]
        if champion["name"] != "system_baseline"
        else ["system_baseline"],
    }
