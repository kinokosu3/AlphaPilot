"""Tests for Tushare download helpers and download_tushare_data (mocked API)."""

from __future__ import annotations

import os
import json
from pathlib import Path

import pandas as pd
import pytest

from alphapilot.systems.data.download_state import DownloadStateStore
from alphapilot.systems.data.prepare_tushare import (
    TUSHARE_SOURCE,
    _get_tushare_client,
    _normalize_daily_frame,
    _normalize_factor_frame,
    _query_trade_dates,
    baostock_to_tushare,
    download_tushare_data,
    ensure_tushare_benchmark_index,
    tushare_to_baostock,
)


class _FakeTushareClient:
    def __init__(
        self,
        *,
        trade_dates: list[str] | None = None,
        daily_rows: dict[str, list[dict]] | None = None,
        adj_rows: dict[str, list[dict]] | None = None,
        basic_rows: dict[str, list[dict]] | None = None,
        fail_codes: set[str] | None = None,
        basic_fail_codes: set[str] | None = None,
    ) -> None:
        self.trade_dates = trade_dates or ["2026-06-10", "2026-06-11", "2026-06-12"]
        self.daily_rows = daily_rows or {}
        self.adj_rows = adj_rows or {}
        self.basic_rows = basic_rows or {}
        self.fail_codes = fail_codes or set()
        self.basic_fail_codes = basic_fail_codes or set()
        self.daily_calls: list[dict] = []
        self.adj_calls: list[dict] = []
        self.basic_calls: list[dict] = []
        self.stock_basic_calls: list[str] = []
        self.index_daily_calls: list[dict] = []

    def stock_basic(
        self, exchange: str, list_status: str, fields: str
    ) -> pd.DataFrame:
        del exchange, fields
        self.stock_basic_calls.append(list_status)
        rows = {
            "L": [{"ts_code": "000001.SZ", "list_status": "L", "list_date": "19910403"}],
            "D": [{"ts_code": "600001.SH", "list_status": "D", "list_date": "19901219", "delist_date": "20050404"}],
            "P": [{"ts_code": "000002.SZ", "list_status": "P", "list_date": "19910129"}],
        }
        return pd.DataFrame(rows.get(list_status, []))

    def trade_cal(self, exchange: str, start_date: str, end_date: str) -> pd.DataFrame:
        rows = []
        for date in self.trade_dates:
            ymd = date.replace("-", "")
            if start_date <= ymd <= end_date:
                rows.append({"cal_date": ymd, "is_open": 1})
        return pd.DataFrame(rows)

    def daily(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        self.daily_calls.append(
            {"ts_code": ts_code, "start_date": start_date, "end_date": end_date}
        )
        if ts_code in self.fail_codes:
            raise RuntimeError(f"daily failed for {ts_code}")
        return pd.DataFrame(self.daily_rows.get(ts_code, []))

    def index_daily(
        self, ts_code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        self.index_daily_calls.append(
            {"ts_code": ts_code, "start_date": start_date, "end_date": end_date}
        )
        return pd.DataFrame(self.daily_rows.get(ts_code, []))

    def adj_factor(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        self.adj_calls.append(
            {"ts_code": ts_code, "start_date": start_date, "end_date": end_date}
        )
        if ts_code in self.fail_codes:
            raise RuntimeError(f"adj_factor failed for {ts_code}")
        return pd.DataFrame(self.adj_rows.get(ts_code, []))

    def daily_basic(
        self, ts_code: str, start_date: str, end_date: str, fields: str = ""
    ) -> pd.DataFrame:
        self.basic_calls.append(
            {
                "ts_code": ts_code,
                "start_date": start_date,
                "end_date": end_date,
                "fields": fields,
            }
        )
        if ts_code in self.basic_fail_codes:
            raise RuntimeError(f"daily_basic gated for {ts_code} (积分要求2000)")
        return pd.DataFrame(self.basic_rows.get(ts_code, []))


def _sample_daily_rows(*dates: str) -> list[dict]:
    return [
        {
            "trade_date": date.replace("-", ""),
            "open": 10.0,
            "high": 10.5,
            "low": 9.8,
            "close": 10.2,
            "pre_close": 10.0,
            "vol": 12345.0,
            "amount": 9876.5,
            "pct_chg": 2.0,
        }
        for date in dates
    ]


def _sample_adj_rows(*dates: str, factor: float = 1.5) -> list[dict]:
    return [
        {"trade_date": date.replace("-", ""), "adj_factor": factor} for date in dates
    ]


def _sample_basic_rows(*dates: str) -> list[dict]:
    return [
        {
            "ts_code": "000001.SZ",
            "trade_date": date.replace("-", ""),
            "turnover_rate": 1.23,
            "pe_ttm": 11.1,
            "pb": 1.4,
            "ps_ttm": 2.6,
        }
        for date in dates
    ]


def test_kernel_config_imports_without_circular_error() -> None:
    from alphapilot.kernel.config import AppConfig, DataConfig

    cfg = DataConfig()
    assert cfg.qlib_data_dir.name == "qlib"
    assert AppConfig().data.qlib_data_dir == cfg.qlib_data_dir


def test_baostock_tushare_code_conversion() -> None:
    assert baostock_to_tushare("sz.000001") == "000001.SZ"
    assert baostock_to_tushare("sh.600000") == "600000.SH"
    assert tushare_to_baostock("000001.SZ") == "sz.000001"
    assert tushare_to_baostock("600000.SH") == "sh.600000"


def test_normalize_daily_frame_maps_schema() -> None:
    raw = pd.DataFrame(_sample_daily_rows("2026-06-10"))
    out = _normalize_daily_frame(raw, "sz.000001")
    assert list(out.columns) == [
        "date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "turn",
        "tradestatus",
        "pctChg",
        "peTTM",
        "pbMRQ",
        "psTTM",
        "pcfNcfTTM",
        "isST",
    ]
    assert out.iloc[0]["code"] == "sz000001"
    assert out.iloc[0]["date"] == "2026-06-10"
    assert out.iloc[0]["volume"] == 1_234_500.0
    assert out.iloc[0]["amount"] == 9_876_500.0


def test_normalize_factor_frame_maps_schema() -> None:
    raw = pd.DataFrame(_sample_adj_rows("2026-06-10", "2026-06-11", factor=3.0))
    out = _normalize_factor_frame(raw, "sz.000001")
    assert out.iloc[-1]["backAdjustFactor"] == 3.0
    assert out.iloc[-1]["foreAdjustFactor"] == pytest.approx(1.0)
    assert out.iloc[0]["foreAdjustFactor"] == pytest.approx(1.0)


def test_query_trade_dates_from_fake_client() -> None:
    client = _FakeTushareClient(trade_dates=["2026-06-10", "2026-06-12"])
    dates = _query_trade_dates(client, "2026-06-10", "2026-06-12")
    assert dates == {"2026-06-10", "2026-06-12"}


def test_get_tushare_client_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="TUSHARE_TOKEN"):
        _get_tushare_client()


def test_download_tushare_data_writes_csv_and_state(tmp_path: Path) -> None:
    code = "sz.000001"
    ts_code = "000001.SZ"
    output_dir = tmp_path / "raw"
    factor_dir = tmp_path / "factors"
    state_path = tmp_path / "download_state.csv"

    client = _FakeTushareClient(
        daily_rows={ts_code: _sample_daily_rows("2026-06-10", "2026-06-11")},
        adj_rows={ts_code: _sample_adj_rows("2026-06-10", "2026-06-11")},
    )

    codes = download_tushare_data(
        start_date="2016-12-31",
        end_date="2026-06-12",
        output_dir=output_dir,
        symbols=[code],
        factor_dir=factor_dir,
        download_state_path=state_path,
        client=client,
    )

    assert codes == [code]
    price_file = output_dir / "sz000001.csv"
    factor_file = factor_dir / "sz000001.csv"
    assert price_file.is_file()
    assert factor_file.is_file()

    price_df = pd.read_csv(price_file)
    assert len(price_df) == 2
    assert price_df["date"].tolist() == ["2026-06-10", "2026-06-11"]

    store = DownloadStateStore(state_path)
    record = store.get(
        source=TUSHARE_SOURCE,
        adjust_mode="none",
        code=code,
        raw_dir=output_dir,
    )
    assert record is not None
    assert record.data_end_date == "2026-06-11"
    assert record.checked_until == "2026-06-12"
    assert record.last_status == "updated"
    assert len(client.daily_calls) == 1


def test_download_tushare_data_skips_up_to_date(tmp_path: Path) -> None:
    code = "sz.000001"
    ts_code = "000001.SZ"
    output_dir = tmp_path / "raw"
    factor_dir = tmp_path / "factors"
    state_path = tmp_path / "download_state.csv"

    store = DownloadStateStore(state_path)
    store.upsert(
        source=TUSHARE_SOURCE,
        adjust_mode="none",
        code=code,
        raw_dir=output_dir,
        data_end_date="2026-06-12",
        checked_until="2026-06-12",
        last_status="updated",
    )
    store.save()

    client = _FakeTushareClient(
        daily_rows={ts_code: _sample_daily_rows("2026-06-12")},
        adj_rows={ts_code: _sample_adj_rows("2026-06-12")},
    )

    download_tushare_data(
        start_date="2016-12-31",
        end_date="2026-06-12",
        output_dir=output_dir,
        symbols=[code],
        factor_dir=factor_dir,
        download_state_path=state_path,
        client=client,
    )

    assert client.daily_calls == []
    assert not (output_dir / "sz000001.csv").exists()


def test_download_tushare_data_incremental_merge(tmp_path: Path) -> None:
    code = "sz.000001"
    ts_code = "000001.SZ"
    output_dir = tmp_path / "raw"
    factor_dir = tmp_path / "factors"
    state_path = tmp_path / "download_state.csv"
    price_file = output_dir / "sz000001.csv"
    output_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "date": ["2026-06-09"],
            "code": ["sz000001"],
            "open": [9.9],
            "high": [10.0],
            "low": [9.8],
            "close": [9.95],
            "preclose": [9.9],
            "volume": [1000],
            "amount": [500],
            "turn": [pd.NA],
            "tradestatus": [1],
            "pctChg": [0.5],
            "peTTM": [pd.NA],
            "pbMRQ": [pd.NA],
            "psTTM": [pd.NA],
            "pcfNcfTTM": [pd.NA],
            "isST": [pd.NA],
        }
    ).to_csv(price_file, index=False)

    store = DownloadStateStore(state_path)
    store.upsert(
        source=TUSHARE_SOURCE,
        adjust_mode="none",
        code=code,
        raw_dir=output_dir,
        data_end_date="2026-06-09",
        checked_until="2026-06-09",
        last_status="updated",
    )
    store.save()

    client = _FakeTushareClient(
        daily_rows={ts_code: _sample_daily_rows("2026-06-10")},
        adj_rows={ts_code: _sample_adj_rows("2026-06-10")},
    )

    download_tushare_data(
        start_date="2016-12-31",
        end_date="2026-06-12",
        output_dir=output_dir,
        symbols=[code],
        factor_dir=factor_dir,
        download_state_path=state_path,
        client=client,
    )

    merged = pd.read_csv(price_file)
    assert merged["date"].tolist() == ["2026-06-09", "2026-06-10"]
    assert len(client.daily_calls) == 1
    assert client.daily_calls[0]["start_date"] == "20260610"


def test_download_tushare_data_fills_daily_basic(tmp_path: Path) -> None:
    code = "sz.000001"
    ts_code = "000001.SZ"
    output_dir = tmp_path / "raw"
    factor_dir = tmp_path / "factors"
    state_path = tmp_path / "download_state.csv"

    client = _FakeTushareClient(
        daily_rows={ts_code: _sample_daily_rows("2026-06-10", "2026-06-11")},
        adj_rows={ts_code: _sample_adj_rows("2026-06-10", "2026-06-11")},
        basic_rows={ts_code: _sample_basic_rows("2026-06-10", "2026-06-11")},
    )

    download_tushare_data(
        start_date="2016-12-31",
        end_date="2026-06-12",
        output_dir=output_dir,
        symbols=[code],
        factor_dir=factor_dir,
        download_state_path=state_path,
        include_daily_basic=True,
        client=client,
    )

    df = pd.read_csv(output_dir / "sz000001.csv")
    assert df["turn"].tolist() == [1.23, 1.23]
    assert df["peTTM"].tolist() == [11.1, 11.1]
    assert df["pbMRQ"].tolist() == [1.4, 1.4]
    assert df["psTTM"].tolist() == [2.6, 2.6]
    # pcfNcfTTM / isST are not in daily_basic -> stay empty
    assert df["pcfNcfTTM"].isna().all()
    # daily_basic is pulled over the full [start_date, end_date] window
    assert len(client.basic_calls) == 1
    assert client.basic_calls[0]["start_date"] == "20161231"


def test_download_tushare_data_without_daily_basic_leaves_na(tmp_path: Path) -> None:
    code = "sz.000001"
    ts_code = "000001.SZ"
    output_dir = tmp_path / "raw"
    factor_dir = tmp_path / "factors"
    state_path = tmp_path / "download_state.csv"

    client = _FakeTushareClient(
        daily_rows={ts_code: _sample_daily_rows("2026-06-10")},
        adj_rows={ts_code: _sample_adj_rows("2026-06-10")},
        basic_rows={ts_code: _sample_basic_rows("2026-06-10")},
    )

    download_tushare_data(
        start_date="2016-12-31",
        end_date="2026-06-12",
        output_dir=output_dir,
        symbols=[code],
        factor_dir=factor_dir,
        download_state_path=state_path,
        client=client,  # include_daily_basic defaults to False
    )

    df = pd.read_csv(output_dir / "sz000001.csv")
    assert df["turn"].isna().all()
    assert df["peTTM"].isna().all()
    assert client.basic_calls == []


def test_download_tushare_data_daily_basic_degrades_on_error(tmp_path: Path) -> None:
    code = "sz.000001"
    ts_code = "000001.SZ"
    output_dir = tmp_path / "raw"
    factor_dir = tmp_path / "factors"
    state_path = tmp_path / "download_state.csv"

    client = _FakeTushareClient(
        daily_rows={ts_code: _sample_daily_rows("2026-06-10", "2026-06-11")},
        adj_rows={ts_code: _sample_adj_rows("2026-06-10", "2026-06-11")},
        basic_fail_codes={ts_code},  # simulate insufficient points / rate limit
    )

    codes = download_tushare_data(
        start_date="2016-12-31",
        end_date="2026-06-12",
        output_dir=output_dir,
        symbols=[code],
        factor_dir=factor_dir,
        download_state_path=state_path,
        include_daily_basic=True,
        client=client,
    )

    # daily_basic failure must not abort price / factor download
    assert codes == [code]
    df = pd.read_csv(output_dir / "sz000001.csv")
    assert df["date"].tolist() == ["2026-06-10", "2026-06-11"]
    assert (factor_dir / "sz000001.csv").is_file()
    assert df["turn"].isna().all()


def test_download_tushare_data_parallel_price_factor_splits_factor_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = "sz.000001"
    ts_code = "000001.SZ"
    output_dir = tmp_path / "raw"
    factor_dir = tmp_path / "factors"
    state_path = tmp_path / "download_state.csv"

    client = _FakeTushareClient(
        daily_rows={ts_code: _sample_daily_rows("2026-06-10")},
        adj_rows={ts_code: _sample_adj_rows("2026-06-10")},
    )
    started = {}

    def fake_start(codes, start_date, end_date, factor_path, token):
        started["codes"] = codes
        started["start_date"] = start_date
        started["end_date"] = end_date
        started["factor_path"] = factor_path
        started["token"] = token
        return object(), object()

    def fake_finish(_process, _queue):
        return {"factor_updated": 1, "errors": 0}

    monkeypatch.setattr(
        "alphapilot.systems.data.prepare_tushare._start_parallel_tushare_factor_process",
        fake_start,
    )
    monkeypatch.setattr(
        "alphapilot.systems.data.prepare_tushare._finish_parallel_tushare_factor_process",
        fake_finish,
    )

    download_tushare_data(
        start_date="2016-12-31",
        end_date="2026-06-12",
        output_dir=output_dir,
        symbols=[code],
        factor_dir=factor_dir,
        download_state_path=state_path,
        client=client,
        parallel_price_factor=True,
    )

    assert started == {
        "codes": [code],
        "start_date": "2016-12-31",
        "end_date": "2026-06-12",
        "factor_path": factor_dir,
        "token": None,
    }
    assert len(client.daily_calls) == 1
    assert client.adj_calls == []
    assert (output_dir / "sz000001.csv").is_file()
    assert not (factor_dir / "sz000001.csv").exists()


def test_tushare_adapter_is_registered() -> None:
    from alphapilot.adapters import get_data_source

    adapter = get_data_source("tushare_cn")
    assert adapter.default_output_dir().name == "raw_data_no_adjust"
    assert "tushare" in str(adapter.default_output_dir())


def test_all_market_include_delisted_queries_l_d_p_and_freezes_metadata(
    tmp_path: Path,
) -> None:
    client = _FakeTushareClient(trade_dates=[])
    output_dir = tmp_path / "raw"

    codes = download_tushare_data(
        start_date="2005-01-01",
        end_date="2005-01-02",
        output_dir=output_dir,
        all_market=True,
        include_delisted=True,
        factor_dir=tmp_path / "factors",
        client=client,
    )

    assert client.stock_basic_calls == ["L", "D", "P"]
    assert codes == ["sz.000001", "sh.600001", "sz.000002"]
    metadata = json.loads((output_dir / "_universe_membership.json").read_text())
    assert metadata["statuses"] == ["L", "D", "P"]
    assert metadata["record_count"] == 3
    assert len(metadata["records_sha256"]) == 64
    assert {row["list_status"] for row in metadata["records"]} == {"L", "D", "P"}


def test_tushare_benchmark_is_dumped_with_frozen_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qlib = tmp_path / "qlib"
    (qlib / "calendars").mkdir(parents=True)
    (qlib / "calendars" / "day.txt").write_text(
        "2026-06-10\n2026-06-11\n",
        encoding="utf-8",
    )
    client = _FakeTushareClient(
        daily_rows={
            "000905.SH": _sample_daily_rows("2026-06-10", "2026-06-11")
        }
    )
    captured: dict[str, object] = {}

    class _Dump:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured["kwargs"] = kwargs
            [csv_path] = list(Path(kwargs["data_path"]).glob("*.csv"))
            captured["frame"] = pd.read_csv(csv_path)

        def dump(self) -> None:
            captured["dumped"] = True

    monkeypatch.setattr(
        "alphapilot.systems.data.qlib_convert.DumpDataUpdate",
        _Dump,
    )

    result = ensure_tushare_benchmark_index(
        qlib_dir=qlib,
        start_date="2026-06-10",
        end_date="2026-06-11",
        client=client,
    )

    assert client.index_daily_calls == [
        {
            "ts_code": "000905.SH",
            "start_date": "20260610",
            "end_date": "20260611",
        }
    ]
    frame = captured["frame"]
    assert isinstance(frame, pd.DataFrame)
    assert frame["code"].unique().tolist() == ["sh000905"]
    assert frame["factor"].eq(1.0).all()
    assert captured["dumped"] is True
    metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["source"] == "tushare_cn"
    assert metadata["instrument"] == "SH000905"
    assert len(metadata["fingerprint"]) == 64


def test_include_delisted_rejects_non_all_market_request(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires all_market"):
        download_tushare_data(
            start_date="2005-01-01",
            end_date="2005-01-02",
            output_dir=tmp_path / "raw",
            symbols=["000001.SZ"],
            include_delisted=True,
            client=_FakeTushareClient(trade_dates=[]),
        )


def test_live_tushare_download_single_symbol(tmp_path: Path) -> None:
    """Optional live test; skipped unless TUSHARE_TOKEN is set."""
    if not os.getenv("TUSHARE_TOKEN"):
        pytest.skip("TUSHARE_TOKEN not set")

    output_dir = tmp_path / "live_raw"
    factor_dir = tmp_path / "live_factors"

    codes = download_tushare_data(
        start_date="2026-06-01",
        end_date="2026-06-12",
        output_dir=output_dir,
        symbols=["sz.000001"],
        factor_dir=factor_dir,
    )
    assert codes == ["sz.000001"]
    if not (output_dir / "sz000001.csv").is_file():
        pytest.skip("Tushare live download did not produce data; network/API unavailable")
    assert (output_dir / "sz000001.csv").is_file()
    assert (factor_dir / "sz000001.csv").is_file()
