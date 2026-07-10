from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd


def test_benchmark_dump_repairs_missing_factor_and_writes_finite_factor(tmp_path, monkeypatch) -> None:
    from alphapilot.systems.data import qlib_convert

    qlib = tmp_path / "qlib"
    (qlib / "calendars").mkdir(parents=True)
    (qlib / "calendars" / "day.txt").write_text("2026-07-08\n2026-07-09\n", encoding="utf-8")
    (qlib / "instruments").mkdir()
    (qlib / "instruments" / "all.txt").write_text(
        "SH000905\t2026-07-08\t2026-07-09\n", encoding="utf-8"
    )
    feature = qlib / "features" / "sh000905"
    feature.mkdir(parents=True)
    (feature / "close.day.bin").touch()  # existing legacy dump, no factor.day.bin

    class Result:
        error_code = "0"
        _rows = iter(
            [
                ["2026-07-08", "sh.000905", "10", "11", "9", "10.5", "10", "100", "1000", "5"],
                ["2026-07-09", "sh.000905", "10.5", "12", "10", "11", "10.5", "200", "2100", "4.7"],
            ]
        )

        def next(self):
            try:
                self.current = next(self._rows)
                return True
            except StopIteration:
                return False

        def get_row_data(self):
            return self.current

    fake_bs = types.SimpleNamespace(
        login=lambda: types.SimpleNamespace(error_code="0", error_msg=""),
        logout=lambda: None,
        query_history_k_data_plus=lambda *a, **k: Result(),
    )
    monkeypatch.setitem(sys.modules, "baostock", fake_bs)

    captured = {}

    class FakeDump:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def dump(self):
            csv_file = next(iter(Path(captured["data_path"]).glob("*.csv")))
            captured["frame"] = pd.read_csv(csv_file)

    monkeypatch.setattr(qlib_convert, "DumpDataUpdate", FakeDump)
    qlib_convert.ensure_benchmark_index(qlib, start_date="2026-07-08", end_date="2026-07-09")

    assert "SH000905" not in (qlib / "instruments" / "all.txt").read_text(encoding="utf-8")
    assert captured["include_fields"].endswith(",factor")
    assert captured["frame"]["factor"].tolist() == [1.0, 1.0]
