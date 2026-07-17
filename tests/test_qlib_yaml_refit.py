from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from alphapilot.systems.backtest.qlib_yaml.generator import render_yaml_text
from alphapilot.systems.backtest.qlib_yaml.schema import QlibYamlParams
from alphapilot.systems.backtest.qlib_yaml.validator import run_static_validation


def _refit_params() -> dict[str, object]:
    return {
        **QlibYamlParams.defaults_for("combined").model_dump(),
        "end_time": "2026-07-16",
        "train_start": "2017-01-01",
        "train_end": "2026-07-08",
        "valid_start": "2026-01-01",
        "valid_end": "2026-06-30",
        "test_start": "2026-07-01",
        "test_end": "2026-07-08",
        "backtest_start": "2026-07-01",
        "backtest_end": "2026-07-08",
    }


def test_overlapping_segments_are_only_allowed_for_final_all_labeled_refit() -> None:
    with pytest.raises(ValidationError, match="train_end must be before valid_start"):
        QlibYamlParams.model_validate(_refit_params())

    params = QlibYamlParams.model_validate(
        {**_refit_params(), "refit_all_labeled": True}
    )
    assert params.train_end == params.test_end


def test_rendered_final_refit_declares_and_validates_its_overlap(tmp_path: Path) -> None:
    params = QlibYamlParams.model_validate(
        {**_refit_params(), "refit_all_labeled": True}
    )
    config = tmp_path / "refit.yaml"
    config.write_text(render_yaml_text(params), encoding="utf-8")
    rendered = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert rendered["alphapilot_refit_all_labeled"] is True
    train_end = rendered["task"]["dataset"]["kwargs"]["segments"]["train"][1]
    assert str(train_end) == "2026-07-08"

    report = run_static_validation(config, workspace=tmp_path)
    segment_check = next(check for check in report.checks if check.name == "segment_order")
    assert segment_check.ok is True
    assert "covers all" in segment_check.message
