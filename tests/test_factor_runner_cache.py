from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from alphapilot.systems.backtest.runners.factor_runner import QlibFactorRunner, _backtest_cacheable


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
