from __future__ import annotations

import pandas as pd
import pytest

from alphapilot.modules.backtest_viz.charts import cum_series, nav_return_series
from alphapilot.systems.backtest.artifacts import build_nav_returns, build_summary


@pytest.fixture()
def report() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "return": [0.10, -0.10],
            "cost": [0.01, 0.00],
            "bench": [0.02, 0.01],
            "turnover": [0.20, 0.40],
            "account": [1090.0, 981.0],
        },
        index=pd.to_datetime(["2026-01-05", "2026-01-06"]),
    )


def test_build_nav_returns_compounds_daily_returns(report: pd.DataFrame) -> None:
    result = build_nav_returns(report)

    assert result["策略(不含成本)"].tolist() == pytest.approx([0.10, -0.01])
    assert result["策略(含成本)"].tolist() == pytest.approx([0.09, -0.019])
    assert result["基准"].tolist() == pytest.approx([0.02, 0.0302])
    assert result["超额(不含成本)"].iloc[-1] == pytest.approx(0.99 / 1.0302 - 1.0)
    assert result["超额(含成本)"].iloc[-1] == pytest.approx(0.981 / 1.0302 - 1.0)

    # The old arithmetic sum would be 0%; the NAV result correctly preserves compounding.
    assert report["return"].sum() == pytest.approx(0.0)
    assert result["策略(不含成本)"].iloc[-1] == pytest.approx(-0.01)


def test_build_summary_uses_net_nav_and_nav_drawdown(report: pd.DataFrame) -> None:
    summary = build_summary(report)

    assert summary["净值收益(不含成本)"] == pytest.approx(-0.01)
    assert summary["净值收益(含成本)"] == pytest.approx(-0.019)
    assert summary["基准净值收益"] == pytest.approx(0.0302)
    assert summary["超额净值收益(不含成本)"] == pytest.approx(0.99 / 1.0302 - 1.0)
    assert summary["最大回撤(不含成本)"] == pytest.approx(-0.10)
    assert summary["最大回撤(含成本)"] == pytest.approx(-0.10)
    assert summary["平均日换手"] == pytest.approx(0.30)
    assert summary["期末总资产"] == pytest.approx(981.0)
    assert summary["累计收益(含成本)"] == summary["净值收益(含成本)"]
    assert summary["基准累计收益"] == summary["基准净值收益"]


def test_drawdown_includes_the_initial_nav() -> None:
    summary = build_summary(
        pd.DataFrame({"return": [-0.20, 0.10], "cost": [0.0, 0.0]})
    )

    assert summary["最大回撤(不含成本)"] == pytest.approx(-0.20)
    assert summary["最大回撤(含成本)"] == pytest.approx(-0.20)


def test_chart_series_uses_the_shared_nav_calculation(report: pd.DataFrame) -> None:
    expected = build_nav_returns(report)
    pd.testing.assert_frame_equal(nav_return_series(report), expected)
    pd.testing.assert_frame_equal(cum_series(report), expected)


def test_nav_summary_is_stable_for_empty_or_missing_optional_columns() -> None:
    report = pd.DataFrame({"return": [0.05, -0.02]})
    result = build_nav_returns(report)
    summary = build_summary(report)

    assert result["策略(不含成本)"].iloc[-1] == pytest.approx(1.05 * 0.98 - 1.0)
    assert result["策略(含成本)"].iloc[-1] == pytest.approx(1.05 * 0.98 - 1.0)
    assert result["基准"].iloc[-1] == pytest.approx(0.0)
    assert summary["净值收益(含成本)"] == pytest.approx(1.05 * 0.98 - 1.0)

    empty = build_summary(pd.DataFrame())
    assert empty["净值收益(含成本)"] == 0.0
    assert empty["最大回撤(含成本)"] == 0.0
