from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from alphapilot.systems.data.pit_validation import validate_pit_dataset


def _records_hash(records: list[dict[str, str]]) -> str:
    payload = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _build_dataset(tmp_path: Path) -> tuple[Path, Path, Path]:
    qlib = tmp_path / "qlib"
    raw = tmp_path / "raw"
    raw.mkdir()
    (qlib / "instruments").mkdir(parents=True)
    (qlib / "calendars").mkdir()
    records = [
        {
            "ts_code": "600000.SH",
            "name": "active",
            "list_status": "L",
            "list_date": "20200102",
            "delist_date": "",
        },
        {
            "ts_code": "000001.SZ",
            "name": "delisted",
            "list_status": "D",
            "list_date": "20200102",
            "delist_date": "20200103",
        },
    ]
    metadata = {
        "schema_version": 1,
        "include_delisted": True,
        "statuses": ["L", "D", "P"],
        "record_count": len(records),
        "records": records,
        "records_sha256": _records_hash(records),
    }
    metadata_path = raw / "_universe_membership.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    (qlib / "instruments" / "main_stock_pit.txt").write_text(
        "SH600000\t2020-01-02\t2020-01-06\n"
        "SZ000001\t2020-01-02\t2020-01-03\n",
        encoding="utf-8",
    )
    (qlib / "calendars" / "day.txt").write_text(
        "2020-01-02\n2020-01-03\n2020-01-06\n",
        encoding="utf-8",
    )
    bars = {
        "SH600000": [("2020-01-02", 10.0), ("2020-01-06", 10.2)],
        "SZ000001": [("2020-01-02", 8.0), ("2020-01-03", 7.9)],
    }
    for symbol, rows in bars.items():
        pd.DataFrame(
            {
                "date": [row[0] for row in rows],
                "open": [row[1] for row in rows],
                "high": [row[1] + 0.1 for row in rows],
                "low": [row[1] - 0.1 for row in rows],
                "close": [row[1] for row in rows],
                "volume": [100.0] * len(rows),
            }
        ).to_csv(raw / f"{symbol.lower()}.csv", index=False)
        feature_dir = qlib / "features" / symbol.lower()
        feature_dir.mkdir(parents=True)
        for feature in ("open", "close", "high", "low", "volume"):
            (feature_dir / f"{feature}.day.bin").write_bytes(b"feature")
    benchmark_dir = qlib / "features" / "sh000905"
    benchmark_dir.mkdir(parents=True)
    (benchmark_dir / "close.day.bin").write_bytes(b"benchmark")
    benchmark_metadata = {
        "schema_version": 1,
        "source": "tushare_cn",
        "instrument": "SH000905",
        "ts_code": "000905.SH",
        "start_date": "2020-01-02",
        "end_date": "2020-01-06",
        "row_count": 3,
        "data_sha256": "benchmark-data",
    }
    benchmark_metadata["fingerprint"] = hashlib.sha256(
        json.dumps(
            benchmark_metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    (qlib / "_benchmark_SH000905.json").write_text(
        json.dumps(benchmark_metadata),
        encoding="utf-8",
    )
    return qlib, raw, metadata_path


def test_validate_pit_dataset_accepts_lifecycle_bound_data(tmp_path: Path) -> None:
    qlib, raw, metadata = _build_dataset(tmp_path)

    report = validate_pit_dataset(
        qlib_dir=qlib,
        raw_dir=raw,
        metadata_path=metadata,
        as_of="2020-01-06",
    )

    assert report["ok"] is True
    assert report["instrument_count"] == 2
    assert report["delisted_member_count"] == 1
    assert report["unique_delist_date_count"] == 1
    assert report["factor_data_fingerprint"]


def test_validate_pit_dataset_rejects_tampering_and_post_delist_bars(
    tmp_path: Path,
) -> None:
    qlib, raw, metadata_path = _build_dataset(tmp_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["records"][0]["name"] = "tampered"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    delisted = pd.read_csv(raw / "sz000001.csv")
    delisted.loc[len(delisted)] = ["2020-01-06", 8.0, 8.1, 7.9, 8.0, 100]
    delisted.to_csv(raw / "sz000001.csv", index=False)

    report = validate_pit_dataset(
        qlib_dir=qlib,
        raw_dir=raw,
        metadata_path=metadata_path,
        as_of="2020-01-06",
        strict=False,
    )

    assert report["ok"] is False
    assert any("records_sha256" in error for error in report["errors"])
    assert any("outside membership lifecycle" in error for error in report["errors"])
    with pytest.raises(ValueError, match="PIT dataset validation failed"):
        validate_pit_dataset(
            qlib_dir=qlib,
            raw_dir=raw,
            metadata_path=metadata_path,
            as_of="2020-01-06",
        )


def test_validate_pit_dataset_rejects_nonfinite_volume_and_invalid_ohlc(
    tmp_path: Path,
) -> None:
    qlib, raw, metadata_path = _build_dataset(tmp_path)
    active = pd.read_csv(raw / "sh600000.csv")
    active.loc[0, "volume"] = float("inf")
    active.loc[1, "high"] = active.loc[1, "close"] - 1
    active.to_csv(raw / "sh600000.csv", index=False)

    report = validate_pit_dataset(
        qlib_dir=qlib,
        raw_dir=raw,
        metadata_path=metadata_path,
        as_of="2020-01-06",
        strict=False,
    )

    assert report["ok"] is False
    assert any("volume contains non-finite" in error for error in report["errors"])
    assert any("high is below open/close/low" in error for error in report["errors"])
