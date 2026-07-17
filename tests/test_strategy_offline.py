"""Tier 1 (offline): strategy creation from factor names.

Builds a strategy asset from factors already in the zoo and verifies the
record round-trips through the strategy store (create / list / get / delete).
No backtest here — that lives in the slow tier.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def seeded(engine):
    factor = engine.get_system("factor")
    factor.add_factor("sf1", "Mean($close,5)/$close-1")
    factor.add_factor("sf2", "$close/$open-1")
    return engine


def test_create_strategy_from_factors(seeded) -> None:
    strategy = seeded.get_system("strategy")
    record = strategy.create_strategy_from_factors(
        strategy_name="s1", factor_names=["sf1", "sf2"]
    )
    assert record.strategy_name == "s1"
    # Factor names resolve to their DSL expressions.
    assert record.factor_formulas == ["Mean($close,5)/$close-1", "$close/$open-1"]
    assert record.metadata.get("factor_names") == ["sf1", "sf2"]


def test_strategy_list_get_delete(seeded) -> None:
    strategy = seeded.get_system("strategy")
    strategy.create_strategy_from_factors(strategy_name="s1", factor_names=["sf1"])
    strategy.create_strategy_from_factors(strategy_name="s2", factor_names=["sf2"])

    names = {r.strategy_name for r in strategy.list_strategy_records()}
    assert {"s1", "s2"} <= names

    got = strategy.get_strategy("s1")
    assert got is not None and got.strategy_name == "s1"

    assert strategy.delete_strategy("s2") is True
    names_after = {r.strategy_name for r in strategy.list_strategy_records()}
    assert "s2" not in names_after


def test_create_with_unknown_factor_is_rejected(seeded) -> None:
    strategy = seeded.get_system("strategy")
    with pytest.raises(Exception):
        strategy.create_strategy_from_factors(
            strategy_name="bad", factor_names=["does_not_exist"]
        )


def test_strategy_freezes_factor_research_provenance(engine) -> None:
    factor = engine.get_system("factor")
    common = {
        "market": "main_stock_pit",
        "provider_uri": "/pit/provider",
        "factor_data_fingerprint": "pit-hash",
        "hypothesis": "overreaction",
        "mining_round": 2,
        "seed": 101,
        "data_split": {"train": ["2017-01-01", "2021-12-31"]},
        "model_fingerprint": "model-hash",
        "qlib_template_fingerprint": "template-hash",
    }
    assert factor.add_factor(
        "provenance_a", "Std($close,10)/Mean($close,10)", metadata=common
    ).acceptable
    assert factor.add_factor(
        "provenance_b", "Mean($volume,7)/Mean($volume,30)", metadata=common
    ).acceptable

    record = engine.get_system("strategy").create_strategy_from_factors(
        strategy_name="provenance_strategy",
        factor_names=["provenance_a", "provenance_b"],
        market="main_stock_pit",
        yaml_params={
            "provider_uri": "/pit/provider",
            "train_start": "2017-01-01",
            "train_end": "2021-12-31",
        },
    )

    assert record.metadata["provider_uri"] == "/pit/provider"
    assert record.metadata["factor_data_fingerprint"] == "pit-hash"
    assert record.metadata["hypotheses"] == ["overreaction"]
    assert record.metadata["random_seeds"] == [101]
    assert len(record.metadata["factor_asset_fingerprint"]) == 64
    assert all(item["metadata_sha256"] for item in record.metadata["factor_assets"])


def test_strategy_rejects_factors_from_different_data_contexts(engine) -> None:
    factor = engine.get_system("factor")
    assert factor.add_factor(
        "context_a",
        "Mean($high,11)/Mean($low,11)",
        metadata={
            "market": "main_stock_pit",
            "provider_uri": "/pit/a",
            "factor_data_fingerprint": "a",
        },
    ).acceptable
    assert factor.add_factor(
        "context_b",
        "Mean($high,13)/Mean($low,13)",
        metadata={
            "market": "main_stock_pit",
            "provider_uri": "/pit/b",
            "factor_data_fingerprint": "b",
        },
    ).acceptable

    with pytest.raises(ValueError, match="different provider_uri"):
        engine.get_system("strategy").create_strategy_from_factors(
            strategy_name="crossed_context",
            factor_names=["context_a", "context_b"],
            market="main_stock_pit",
        )
