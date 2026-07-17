from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
import hashlib

import pandas as pd


class _PredictModel:
    def predict(self, _dataset):  # noqa: ANN001, ANN201
        index = pd.MultiIndex.from_tuples(
            [(pd.Timestamp("2026-07-16"), "SH600000")],
            names=["datetime", "instrument"],
        )
        return pd.Series([0.75], index=index)


def _factor_context(provider: Path, market: str, fingerprint: str) -> SimpleNamespace:
    return SimpleNamespace(
        fingerprint=fingerprint,
        spec=SimpleNamespace(
            qlib_dir=provider,
            market=market,
            freq="day",
            start_date="2015-01-01",
        ),
        env=lambda: {
            "ALPHAPILOT_FACTOR_DATA_DIR": f"/{fingerprint}/all",
            "ALPHAPILOT_FACTOR_DATA_DEBUG_DIR": f"/{fingerprint}/debug",
            "ALPHAPILOT_FACTOR_DATA_FINGERPRINT": fingerprint,
            "ALPHAPILOT_FACTOR_DATA_MARKET": market,
        },
    )


def test_predict_scores_uses_explicit_context_without_global_h5_contamination(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    import pickle

    from alphapilot.systems.selection import predict

    model = tmp_path / "model.pkl"
    with model.open("wb") as handle:
        pickle.dump(_PredictModel(), handle)
    factors = tmp_path / "factors.csv"
    factors.write_text("factor_name,factor_expression\nf1,$close\n")

    captured: list[tuple[str, str]] = []

    def fake_compute(_factor_csv, **kwargs):  # noqa: ANN001, ANN202
        context = kwargs["factor_data_context"]
        run_env = kwargs["run_env"]
        captured.append((context.fingerprint, run_env["ALPHAPILOT_FACTOR_DATA_FINGERPRINT"]))
        output = tmp_path / f"{context.fingerprint}.pkl"
        frame = pd.DataFrame(
            {("feature", "f1"): [1.0]},
            index=pd.MultiIndex.from_tuples(
                [(pd.Timestamp("2026-07-16"), "SH600000")],
                names=["datetime", "instrument"],
            ),
        )
        with output.open("wb") as handle:
            pickle.dump(frame, handle)
        return output

    monkeypatch.setattr(predict, "compute_combined_factors", fake_compute)
    monkeypatch.setattr(predict, "build_dataset_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(predict, "_init_qlib", lambda *_args, **_kwargs: None)
    fake_qlib = ModuleType("qlib")
    fake_utils = ModuleType("qlib.utils")
    fake_utils.init_instance_by_config = lambda _config: object()  # type: ignore[attr-defined]
    fake_qlib.utils = fake_utils  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "qlib", fake_qlib)
    monkeypatch.setitem(sys.modules, "qlib.utils", fake_utils)
    monkeypatch.setenv("ALPHAPILOT_FACTOR_DATA_FINGERPRINT", "rogue-global")

    for suffix in ("a", "b"):
        provider = (tmp_path / f"qlib-{suffix}").resolve()
        provider.mkdir()
        market = f"market_{suffix}"
        runtime_fingerprint = f"runtime-{suffix}"
        scores = predict.predict_scores(
            "2026-07-16",
            model,
            factors,
            yaml_params={
                "template_type": "combined",
                "market": market,
                "provider_uri": str(provider),
            },
            provider_uri=str(provider),
            market=market,
            factor_data_fingerprint=f"training-{suffix}",
            factor_data_context=_factor_context(
                provider,
                market,
                runtime_fingerprint,
            ),
        )
        assert scores.iloc[0] == 0.75
        assert scores.attrs["factor_values_hash"]
        assert scores.attrs["full_score_hash"]
        assert scores.attrs["runtime_factor_data_fingerprint"] == runtime_fingerprint

    assert captured == [
        ("runtime-a", "runtime-a"),
        ("runtime-b", "runtime-b"),
    ]


def test_research_snapshot_preserves_provider_market_and_training_fingerprint(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    from alphapilot.systems.strategy import StrategyModelSpec, StrategyRecord
    from alphapilot.systems.trading.artifacts import ResearchArtifactSnapshotter

    strategy_dir = tmp_path / "strategy"
    strategy_dir.mkdir()
    model = strategy_dir / "model.pkl"
    model.write_bytes(b"trusted-model")
    provider = (tmp_path / "qlib").resolve()
    provider.mkdir()
    record = StrategyRecord(
        strategy_name="champion",
        factor_formulas=["$close/$open-1"],
        model=StrategyModelSpec(
            model_name="LGBModel",
            trained_artifact_uri=str(model),
        ),
        metadata={
            "market": "main_stock_pit",
            "provider_uri": str(provider),
            "factor_data_fingerprint": "training-fingerprint",
            "yaml_params": {"template_type": "combined"},
        },
    )

    strategy_system = SimpleNamespace(
        get_strategy=lambda _name: record,
        param_database=SimpleNamespace(strategy_dir=lambda _name: strategy_dir),
    )
    config = SimpleNamespace(
        data=SimpleNamespace(qlib_data_dir=tmp_path / "wrong-default"),
        backtest=SimpleNamespace(use_local=True),
    )
    monkeypatch.setenv("ALPHAPILOT_TRUSTED_MODEL_DIRS", str(strategy_dir))

    binding = ResearchArtifactSnapshotter(
        tmp_path / "snapshots",
        strategy_system=strategy_system,
        config=config,
    ).snapshot(
        strategy_name="champion",
        instance_id="champion-live",
        universe=["SH600000", "SZ000001"],
    )

    assert binding["provider_uri"] == str(provider)
    assert binding["market"] == "main_stock_pit"
    assert binding["factor_data_fingerprint"] == "training-fingerprint"
    assert binding["yaml_params"]["provider_uri"] == str(provider)
    assert binding["yaml_params"]["market"] == "main_stock_pit"
    assert binding["binding_hash"]

    from alphapilot.systems.trading.artifacts import verify_artifact_binding

    tampered = {**binding, "factor_data_fingerprint": "changed"}
    try:
        verify_artifact_binding(
            tampered,
            snapshot_root=tmp_path / "snapshots",
            expected_instance_id="champion-live",
        )
    except ValueError as exc:
        assert "metadata has changed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("tampered data fingerprint was accepted")


def test_research_snapshot_rejects_changes_to_frozen_research_asset(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    from alphapilot.systems.strategy import StrategyModelSpec, StrategyRecord
    from alphapilot.systems.trading.artifacts import (
        ResearchArtifactSnapshotter,
        _canonical_json_sha256,
    )

    strategy_dir = tmp_path / "strategy"
    template_dir = strategy_dir / "qlib_template"
    template_dir.mkdir(parents=True)
    model = strategy_dir / "artifacts" / "fitted_model.pkl"
    model.parent.mkdir()
    model.write_bytes(b"frozen-model")
    config = template_dir / "workflow.yaml"
    config.write_text("model: LightGBM\n", encoding="utf-8")
    provider = tmp_path / "qlib"
    provider.mkdir()
    formulas = ["$close/$open-1"]

    def record() -> StrategyRecord:
        return StrategyRecord(
            strategy_name="champion",
            factor_formulas=list(formulas),
            model=StrategyModelSpec(
                model_name="LGBModel",
                trained_artifact_uri=str(model),
            ),
            metadata={
                "market": "main_stock_pit",
                "provider_uri": str(provider.resolve()),
                "model_hash": hashlib.sha256(b"frozen-model").hexdigest(),
                "factor_formula_hash": _canonical_json_sha256(formulas),
                "qlib_config_fingerprint": hashlib.sha256(config.read_bytes()).hexdigest(),
                "qlib_config_path": "qlib_template/workflow.yaml",
                "qlib_template_snapshot_dir": "qlib_template",
            },
        )

    current = record()
    strategy_system = SimpleNamespace(
        get_strategy=lambda _name: current,
        param_database=SimpleNamespace(strategy_dir=lambda _name: strategy_dir),
    )
    snapshotter = ResearchArtifactSnapshotter(
        tmp_path / "snapshots",
        strategy_system=strategy_system,
        config=SimpleNamespace(
            data=SimpleNamespace(qlib_data_dir=provider),
            backtest=SimpleNamespace(use_local=True),
        ),
    )
    monkeypatch.setenv("ALPHAPILOT_TRUSTED_MODEL_DIRS", str(strategy_dir))

    model.write_bytes(b"changed-model")
    try:
        snapshotter.snapshot(
            strategy_name="champion",
            instance_id="model-tamper",
            universe=["SH600000"],
        )
    except ValueError as exc:
        assert "model has changed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("changed frozen model was accepted")

    model.write_bytes(b"frozen-model")
    current.factor_formulas[0] = "$high/$low-1"
    try:
        snapshotter.snapshot(
            strategy_name="champion",
            instance_id="factor-tamper",
            universe=["SH600000"],
        )
    except ValueError as exc:
        assert "factors have changed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("changed frozen factors were accepted")

    current = record()
    config.write_text("model: tampered\n", encoding="utf-8")
    try:
        snapshotter.snapshot(
            strategy_name="champion",
            instance_id="config-tamper",
            universe=["SH600000"],
        )
    except ValueError as exc:
        assert "Qlib config has changed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("changed frozen Qlib config was accepted")
