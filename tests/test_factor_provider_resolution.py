from pathlib import Path
from types import SimpleNamespace

from alphapilot.systems.backtest.pipelines.factor_evaluation import _resolve_factor_qlib_dir


def _context(default: Path) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(data=SimpleNamespace(qlib_data_dir=default)))


def test_request_provider_wins_for_intraday(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("ALPHAPILOT_QLIB_DATA_DIR", raising=False)
    requested = tmp_path / "custom_5min"
    request = SimpleNamespace(yaml_params={"provider_uri": str(requested)})

    resolved = _resolve_factor_qlib_dir(_context(tmp_path / "daily"), request, "5min")

    assert resolved == str(requested)


def test_environment_provider_wins_when_yaml_omits_it(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    configured = tmp_path / "env_5min"
    monkeypatch.setenv("ALPHAPILOT_QLIB_DATA_DIR", str(configured))
    request = SimpleNamespace(yaml_params={"market": "mini"})

    resolved = _resolve_factor_qlib_dir(_context(tmp_path / "daily"), request, "5min")

    assert resolved == str(configured)


def test_daily_context_provider_is_default(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.delenv("ALPHAPILOT_QLIB_DATA_DIR", raising=False)
    default = tmp_path / "daily"

    resolved = _resolve_factor_qlib_dir(_context(default), SimpleNamespace(yaml_params=None), "day")

    assert resolved == str(default)
