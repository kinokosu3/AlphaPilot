from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from alphapilot.systems.backtest.runners.factor_runner import (
    QlibFactorRunner,
    _backtest_cacheable,
    _bind_market_to_workspace_config,
    _coerce_yaml_params,
)


_DEFAULT = object()


def _experiment(workspace, result=_DEFAULT):
    return SimpleNamespace(
        result=pd.Series({"IC": 0.01}) if result is _DEFAULT else result,
        experiment_workspace=SimpleNamespace(workspace_path=workspace),
        based_experiments=[],
    )


def test_backtest_cache_rejects_missing_or_incomplete_workspace(tmp_path) -> None:
    missing = tmp_path / "removed"
    assert not _backtest_cacheable(_experiment(missing))

    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "qlib_res.csv").write_text("IC\n0.1\n", encoding="utf-8")
    assert not _backtest_cacheable(_experiment(incomplete))


def test_backtest_cache_accepts_materialized_portfolio(tmp_path) -> None:
    workspace = tmp_path / "complete"
    workspace.mkdir()
    (workspace / "qlib_res.csv").write_text("IC\n0.1\n", encoding="utf-8")
    (workspace / "ret.pkl").write_bytes(b"artifact-present")
    assert _backtest_cacheable(_experiment(workspace))


def test_backtest_cache_rejects_resultless_experiment(tmp_path) -> None:
    workspace = tmp_path / "complete"
    workspace.mkdir()
    (workspace / "qlib_res.csv").touch()
    (workspace / "ret.pkl").touch()
    assert not _backtest_cacheable(_experiment(workspace, result=None))


def test_cached_backtest_restores_portfolio_artifacts_to_new_workspace(tmp_path) -> None:
    source = tmp_path / "cached"
    destination = tmp_path / "current"
    source.mkdir()
    destination.mkdir()
    (source / "qlib_res.csv").write_text("IC\n0.1\n", encoding="utf-8")
    (source / "ret.pkl").write_bytes(b"portfolio")
    cached = _experiment(source)
    current = _experiment(destination, result=None)

    restored = QlibFactorRunner.__new__(QlibFactorRunner).assign_cached_result(current, cached)

    assert restored.result is cached.result
    assert (destination / "qlib_res.csv").is_file()
    assert (destination / "ret.pkl").read_bytes() == b"portfolio"


def _write_minimal_combined_config(path) -> None:
    path.write_text(
        """\
market: &market main_stock_2026_4_27
data_handler_config: &data_handler_config
    instruments: *market
task: {}
""",
        encoding="utf-8",
    )


def test_market_binding_rewrites_market_and_instruments(tmp_path) -> None:
    config_path = tmp_path / "conf_cn_combined_kdd_ver.yaml"
    _write_minimal_combined_config(config_path)

    _bind_market_to_workspace_config(config_path, "中证1000")

    text = config_path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    assert 'market: &market "中证1000"' in text
    assert doc["market"] == "中证1000"
    assert doc["data_handler_config"]["instruments"] == "中证1000"


def test_market_binding_fails_closed_without_expected_anchor(tmp_path) -> None:
    config_path = tmp_path / "custom.yaml"
    config_path.write_text("market: another_pool\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one"):
        _bind_market_to_workspace_config(config_path, "中证1000")


def test_plain_combined_yaml_patch_does_not_restore_fixed_features() -> None:
    params = _coerce_yaml_params({"market": "中证1000"})

    assert params.template_type == "combined"
    assert params.market == "中证1000"
    assert params.feature_expressions == []


def test_runner_binds_factor_context_market_before_qrun(tmp_path) -> None:
    config_name = "conf_cn_combined_kdd_ver.yaml"
    _write_minimal_combined_config(tmp_path / config_name)

    class FakeWorkspace:
        workspace_path = tmp_path
        executed_doc = None

        def execute(self, *, qlib_config_name, use_local, run_env):
            self.executed_doc = yaml.safe_load(
                (self.workspace_path / qlib_config_name).read_text(encoding="utf-8")
            )
            return pd.Series({"IC": 0.01})

    workspace = FakeWorkspace()
    exp = SimpleNamespace(
        based_experiments=[],
        sub_tasks=[],
        qlib_config_name=config_name,
        experiment_workspace=workspace,
        factor_data_context=SimpleNamespace(spec=SimpleNamespace(market="中证1000")),
        result=None,
    )

    result = QlibFactorRunner.develop.__wrapped__(
        QlibFactorRunner.__new__(QlibFactorRunner), exp, use_local=True, run_env={}
    )

    assert result is exp
    assert workspace.executed_doc["market"] == "中证1000"
    assert workspace.executed_doc["data_handler_config"]["instruments"] == "中证1000"
