from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from alphapilot.systems.data import manage


@pytest.fixture
def isolated_data_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    roots = {
        mode: tmp_path / "raw" / mode
        for mode in ("none", "forward", "backward")
    }
    for root in roots.values():
        root.mkdir(parents=True)

    monkeypatch.setattr(
        manage,
        "_raw_dir",
        lambda mode, _source=None: roots[str(mode)],
    )
    return roots


def _write_prices(path: Path) -> None:
    pd.DataFrame(
        {
            "date": ["2026-07-13", "2026-07-14", "2026-07-15"],
            "code": ["sh600000"] * 3,
            "open": [10.0, 11.0, 12.0],
            "high": [10.5, 11.5, 12.5],
            "low": [9.5, 10.5, 11.5],
            "close": [10.2, 11.2, 12.2],
            "preclose": [9.8, 10.2, 11.2],
        }
    ).to_csv(path, index=False)


def test_list_symbols_uses_only_requested_isolated_modes(
    isolated_data_roots: dict[str, Path],
) -> None:
    _write_prices(isolated_data_roots["none"] / "sh600000.csv")
    _write_prices(isolated_data_roots["forward"] / "sz000001.csv")

    assert manage.list_symbols("none") == {"none": ["sh600000"]}
    assert manage.list_symbols(["forward", "none"]) == {
        "forward": ["sz000001"],
        "none": ["sh600000"],
    }


def test_trim_symbol_dry_run_and_write_are_isolated(
    isolated_data_roots: dict[str, Path],
) -> None:
    csv_path = isolated_data_roots["none"] / "sh600000.csv"
    _write_prices(csv_path)

    preview = manage.trim_symbol(
        "600000.SH",
        adjust_modes="none",
        start="2026-07-14",
        drop_dates="2026-07-15",
        dry_run=True,
    )
    assert preview["modes"]["none"] == {
        "status": "trimmed",
        "before": 3,
        "after": 1,
        "removed": 2,
    }
    assert len(pd.read_csv(csv_path)) == 3

    result = manage.trim_symbol(
        "sh.600000",
        adjust_modes="none",
        start="2026-07-14",
        drop_dates=["2026-07-15"],
    )
    assert result["modes"]["none"]["removed"] == 2
    assert pd.read_csv(csv_path)["date"].tolist() == ["2026-07-14"]


def test_delete_symbol_dry_run_then_removes_all_isolated_layers(
    tmp_path: Path,
    isolated_data_roots: dict[str, Path],
) -> None:
    for mode in ("none", "forward"):
        _write_prices(isolated_data_roots[mode] / "sh600000.csv")

    factor_dir = tmp_path / "factors"
    factor_dir.mkdir()
    (factor_dir / "sh600000.csv").write_text(
        "dividOperateDate,foreAdjustFactor\n2026-07-13,1.0\n",
        encoding="utf-8",
    )

    qlib_dir = tmp_path / "qlib"
    feature_dir = qlib_dir / "features" / "sh600000"
    feature_dir.mkdir(parents=True)
    (feature_dir / "close.day.bin").write_bytes(b"feature")
    instruments = qlib_dir / "instruments"
    instruments.mkdir()
    for name in ("all.txt", "pool.txt"):
        (instruments / name).write_text(
            "SH600000\t2026-07-13\t2026-07-15\n"
            "SZ000001\t2026-07-13\t2026-07-15\n",
            encoding="utf-8",
        )

    preview = manage.delete_symbol(
        "sh600000",
        qlib_dir=qlib_dir,
        factor_dir=factor_dir,
        adjust_modes=["none", "forward"],
        dry_run=True,
    )
    assert preview["dry_run"] is True
    assert (isolated_data_roots["none"] / "sh600000.csv").is_file()
    assert feature_dir.is_dir()
    assert "SH600000" in (instruments / "all.txt").read_text(encoding="utf-8")

    result = manage.delete_symbol(
        "600000.SH",
        qlib_dir=qlib_dir,
        factor_dir=factor_dir,
        adjust_modes=["none", "forward"],
    )
    assert result["instruments_updated"] == ["all.txt", "pool.txt"]
    assert not (isolated_data_roots["none"] / "sh600000.csv").exists()
    assert not (isolated_data_roots["forward"] / "sh600000.csv").exists()
    assert not (factor_dir / "sh600000.csv").exists()
    assert not feature_dir.exists()
    assert "SH600000" not in (instruments / "all.txt").read_text(encoding="utf-8")
    assert "SZ000001" in (instruments / "all.txt").read_text(encoding="utf-8")
