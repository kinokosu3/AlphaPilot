from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pandas as pd


def _load_compare_tool():
    path = Path(__file__).resolve().parents[1] / "scripts" / "compare_tushare_adjust.py"
    spec = importlib.util.spec_from_file_location("compare_tushare_adjust", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cross_source_compare_failure_keeps_required_flag_and_note() -> None:
    tool = _load_compare_tool()
    left = pd.DataFrame({"date": pd.to_datetime(["2026-07-17"]), "close": [10.0]})
    right = pd.DataFrame({"date": pd.to_datetime(["2026-07-18"]), "close": [10.0]})

    result = tool._compare_pair(
        left,
        right,
        label="no-overlap",
        symbol="sh.600000",
        atol=0.01,
        rtol=0.001,
    )

    assert result.passed is False
    assert result.required is True
    assert result.note == "no overlapping dates"
