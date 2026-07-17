from pathlib import Path
import json

import pandas as pd

from alphapilot.systems.data.stock_list import (
    load_stocks_from_file,
    write_qlib_instruments,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_qa_stock_pool_contains_30_unique_symbols() -> None:
    """Keep the named QA universe aligned with its advertised size."""
    path = REPO_ROOT / "important_data" / "stock_lists" / "test_stock_pool_30.csv"

    symbols = load_stocks_from_file(path)

    assert len(symbols) == 30
    assert len(set(symbols)) == 30
    assert {
        "sh.600000",
        "sh.600085",
        "sh.600188",
        "sh.600519",
        "sh.600588",
        "sh.600711",
    } <= set(symbols)


def test_pit_instruments_use_listing_lifecycle_not_last_trade_date(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    pd.DataFrame({
        "date": ["2026-05-29", "2026-06-01"],
    }).to_csv(raw / "sz000001.csv", index=False)
    pd.DataFrame({
        "date": ["2005-03-30", "2005-03-31"],
    }).to_csv(raw / "sh600001.csv", index=False)
    metadata = tmp_path / "membership.json"
    metadata.write_text(json.dumps({
        "records": [
            {
                "ts_code": "000001.SZ",
                "list_status": "L",
                "list_date": "19910403",
                "delist_date": None,
            },
            {
                "ts_code": "600001.SH",
                "list_status": "D",
                "list_date": "19901219",
                "delist_date": "20050404",
            },
        ],
    }))

    output = write_qlib_instruments(
        ["sz.000001", "sh.600001"],
        tmp_path / "qlib",
        "main_stock_pit",
        start_date="2005-01-01",
        end_date="2026-07-16",
        data_dir=raw,
        membership_metadata_path=metadata,
    )

    assert output.read_text().splitlines() == [
        "SZ000001\t2005-01-01\t2026-07-16",
        "SH600001\t2005-01-01\t2005-04-04",
    ]
