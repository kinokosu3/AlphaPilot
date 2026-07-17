from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from alphapilot.systems.research.whitelist import (
    build_live_whitelist,
    freeze_whitelist,
    verify_whitelist,
)


def _bars() -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", periods=130)
    symbols = ["SH600001", "SH600002", "SH600003", "SH600004"]
    index = pd.MultiIndex.from_product(
        [dates, symbols], names=["datetime", "instrument"]
    )
    frame = pd.DataFrame(index=index)
    close = {"SH600001": 10.0, "SH600002": 11.0, "SH600003": 12.0, "SH600004": 30.0}
    amount = {
        "SH600001": 4_000_000.0,
        "SH600002": 9_000_000.0,
        "SH600003": 8_000_000.0,
        "SH600004": 7_000_000.0,
    }
    frame["close"] = [close[symbol] for _, symbol in index]
    frame["volume"] = 100_000.0
    frame["amount"] = [amount[symbol] for _, symbol in index]
    return frame


def test_whitelist_excludes_st_paused_and_oversized_lots() -> None:
    bars = _bars()
    latest = bars.index.get_level_values("datetime").max()
    bars.loc[(latest, "SH600003"), "volume"] = 0
    basic = pd.DataFrame(
        {
            "ts_code": ["600001.SH", "600002.SH", "600003.SH", "600004.SH"],
            "name": ["normal", "ST risky", "paused", "expensive"],
            "list_status": ["L", "L", "L", "L"],
        }
    )

    result = build_live_whitelist(
        bars,
        basic,
        account_equity=100_000,
        as_of=str(latest.date()),
    )

    assert result["symbols"] == ["SH600001"]
    assert len(result["fingerprint"]) == 64
    assert result["records"][0]["one_lot_value"] == 1_000.0
    assert verify_whitelist(result)["ok"] is True

    tampered = {**result, "symbols": ["SH600099"]}
    assert verify_whitelist(tampered)["ok"] is False


def test_frozen_whitelist_cannot_be_overwritten(tmp_path: Path) -> None:
    payload = {"symbols": ["SH600001"], "fingerprint": "hash"}
    target = freeze_whitelist(payload, tmp_path / "whitelist.json")
    assert target.is_file()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        freeze_whitelist(payload, target)
