"""Point-in-time universe and Qlib dataset quality gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from alphapilot.systems.data.factor_h5 import FactorDataSpec
from alphapilot.systems.data.prepare_tushare import BENCHMARK_METADATA_PREFIX
from alphapilot.systems.data.stock_list import normalize_to_baostock


REQUIRED_FEATURES = ("open", "close", "high", "low", "volume")


def _canonical_records_hash(records: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_instruments(path: Path) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    rows: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ValueError(f"invalid instruments row {line_no}: {line!r}")
        rows.append((parts[0].upper(), pd.Timestamp(parts[1]), pd.Timestamp(parts[2])))
    return rows


def validate_pit_dataset(
    *,
    qlib_dir: str | Path,
    raw_dir: str | Path,
    market: str = "main_stock_pit",
    as_of: str = "2026-07-16",
    metadata_path: str | Path | None = None,
    benchmark: str = "SH000905",
    strict: bool = True,
) -> dict[str, Any]:
    """Validate membership lifecycle, raw bars and required Qlib artifacts."""

    qlib_root = Path(qlib_dir).expanduser().resolve()
    raw_root = Path(raw_dir).expanduser().resolve()
    membership_path = Path(
        metadata_path or raw_root / "_universe_membership.json"
    ).expanduser().resolve()
    instrument_path = qlib_root / "instruments" / f"{market}.txt"
    calendar_path = qlib_root / "calendars" / "day.txt"
    errors: list[str] = []
    warnings: list[str] = []

    if not membership_path.is_file():
        raise FileNotFoundError(f"PIT membership metadata is missing: {membership_path}")
    if not instrument_path.is_file():
        raise FileNotFoundError(f"PIT instruments file is missing: {instrument_path}")
    metadata = json.loads(membership_path.read_text(encoding="utf-8"))
    records = list(metadata.get("records") or [])
    if metadata.get("include_delisted") is not True:
        errors.append("membership metadata is not marked include_delisted=true")
    if int(metadata.get("record_count") or -1) != len(records):
        errors.append("membership record_count does not match its records")
    actual_records_hash = _canonical_records_hash(records)
    if actual_records_hash != str(metadata.get("records_sha256") or ""):
        errors.append("membership records_sha256 does not match its records")
    queried_statuses = {
        str(status) for status in (metadata.get("statuses") or []) if str(status)
    }
    if not {"L", "D", "P"}.issubset(queried_statuses):
        errors.append(
            "membership metadata was not built from all L/D/P queries: "
            f"{sorted(queried_statuses)}"
        )

    metadata_by_symbol: dict[str, dict[str, Any]] = {}
    duplicate_membership_symbols: set[str] = set()
    for row in records:
        code = normalize_to_baostock(str(row.get("ts_code") or ""))
        if code:
            symbol = code.replace(".", "").upper()
            if symbol in metadata_by_symbol:
                duplicate_membership_symbols.add(symbol)
            metadata_by_symbol[symbol] = row
    if duplicate_membership_symbols:
        errors.append(
            "membership metadata contains duplicate symbols: "
            + ", ".join(sorted(duplicate_membership_symbols)[:20])
        )

    instruments = _parse_instruments(instrument_path)
    symbols = [row[0] for row in instruments]
    if not instruments:
        errors.append("PIT universe is empty")
    if len(symbols) != len(set(symbols)):
        errors.append("PIT instruments contain duplicate symbols")
    cutoff = pd.Timestamp(as_of)
    delisted_members = 0
    delist_dates: list[pd.Timestamp] = []
    checked_csv = 0
    missing_features = 0

    for symbol, member_start, member_end in instruments:
        if member_start > member_end:
            errors.append(f"{symbol}: membership starts after it ends")
        if member_end > cutoff:
            errors.append(f"{symbol}: membership ends after as_of={as_of}")
        row = metadata_by_symbol.get(symbol)
        if row is None:
            errors.append(f"{symbol}: no stock_basic membership record")
        else:
            list_date = pd.to_datetime(
                row.get("list_date"), format="%Y%m%d", errors="coerce"
            )
            delist_date = pd.to_datetime(
                row.get("delist_date"), format="%Y%m%d", errors="coerce"
            )
            if pd.notna(list_date) and member_start < list_date:
                errors.append(f"{symbol}: membership begins before list_date")
            status = str(row.get("list_status") or "")
            expected_end = min(cutoff, delist_date) if pd.notna(delist_date) else cutoff
            if status in {"L", "P"} and member_end != cutoff:
                errors.append(f"{symbol}: active/paused member does not extend to as_of")
            if status == "D":
                delisted_members += 1
                if pd.isna(delist_date):
                    errors.append(f"{symbol}: delisted member has no valid delist_date")
                else:
                    delist_dates.append(pd.Timestamp(delist_date))
                if pd.notna(delist_date) and member_end != expected_end:
                    errors.append(f"{symbol}: delisted membership end differs from delist_date")

        raw_file = raw_root / f"{symbol.lower()}.csv"
        if not raw_file.is_file():
            errors.append(f"{symbol}: raw CSV is missing")
            continue
        try:
            frame = pd.read_csv(
                raw_file,
                usecols=["date", "open", "high", "low", "close", "volume"],
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{symbol}: raw CSV cannot be read: {exc}")
            continue
        checked_csv += 1
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        if frame.empty or frame["date"].isna().all():
            errors.append(f"{symbol}: raw CSV has no valid dates")
            continue
        if frame["date"].duplicated().any():
            errors.append(f"{symbol}: raw CSV contains duplicate dates")
        if frame["date"].min() < member_start or frame["date"].max() > member_end:
            errors.append(f"{symbol}: raw bars fall outside membership lifecycle")
        numeric = frame[["open", "high", "low", "close", "volume"]].apply(
            pd.to_numeric,
            errors="coerce",
        ).replace([np.inf, -np.inf], np.nan)
        if numeric[["open", "high", "low", "close"]].isna().any().any():
            errors.append(f"{symbol}: prices contain non-finite values")
        if (numeric[["open", "high", "low", "close"]] <= 0).any().any():
            errors.append(f"{symbol}: prices contain non-positive values")
        if numeric["volume"].isna().any():
            errors.append(f"{symbol}: volume contains non-finite values")
        if (numeric["high"] < numeric["low"]).any():
            errors.append(f"{symbol}: high is below low")
        if (
            numeric["high"]
            < numeric[["open", "close", "low"]].max(axis=1)
        ).any():
            errors.append(f"{symbol}: high is below open/close/low")
        if (
            numeric["low"]
            > numeric[["open", "close", "high"]].min(axis=1)
        ).any():
            errors.append(f"{symbol}: low is above open/close/high")
        if (numeric["volume"] < 0).any():
            errors.append(f"{symbol}: volume is negative")

        feature_dir = qlib_root / "features" / symbol.lower()
        for feature in REQUIRED_FEATURES:
            if not (feature_dir / f"{feature}.day.bin").is_file():
                missing_features += 1
                errors.append(f"{symbol}: missing Qlib feature {feature}")

    if delisted_members == 0:
        errors.append("PIT universe contains no delisted member")
    required_unique_delist_dates = min(10, delisted_members)
    unique_delist_dates = len(set(delist_dates))
    if unique_delist_dates < required_unique_delist_dates:
        errors.append(
            "delist dates are insufficiently dispersed: "
            f"{unique_delist_dates} unique for {delisted_members} delisted members"
        )
    if not calendar_path.is_file():
        errors.append("Qlib day calendar is missing")
        calendar_end = None
    else:
        calendar = [line.strip() for line in calendar_path.read_text().splitlines() if line.strip()]
        calendar_end = calendar[-1] if calendar else None
        if calendar_end is None or pd.Timestamp(calendar_end) < cutoff:
            errors.append("Qlib day calendar does not reach as_of")
    benchmark_dir = qlib_root / "features" / benchmark.lower()
    if not (benchmark_dir / "close.day.bin").is_file():
        errors.append(f"benchmark {benchmark} close feature is missing")
    benchmark_metadata_path = (
        qlib_root / f"{BENCHMARK_METADATA_PREFIX}{benchmark.upper()}.json"
    )
    benchmark_metadata: dict[str, Any] = {}
    if not benchmark_metadata_path.is_file():
        errors.append(f"benchmark {benchmark} Tushare provenance is missing")
    else:
        benchmark_metadata = json.loads(
            benchmark_metadata_path.read_text(encoding="utf-8")
        )
        claimed = str(benchmark_metadata.pop("fingerprint", ""))
        actual = hashlib.sha256(
            json.dumps(
                benchmark_metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        benchmark_metadata["fingerprint"] = claimed
        if claimed != actual:
            errors.append(f"benchmark {benchmark} provenance fingerprint changed")
        if benchmark_metadata.get("source") != "tushare_cn":
            errors.append(f"benchmark {benchmark} is not sourced from Tushare")
        if benchmark_metadata.get("instrument") != benchmark.upper():
            errors.append(f"benchmark {benchmark} provenance instrument differs")

    try:
        factor_fingerprint = FactorDataSpec(
            qlib_dir=qlib_root,
            market=market,
        ).fingerprint()
    except Exception as exc:  # noqa: BLE001
        factor_fingerprint = ""
        errors.append(f"factor-data fingerprint failed: {exc}")

    report = {
        "ok": not errors,
        "market": market,
        "as_of": as_of,
        "instrument_count": len(instruments),
        "checked_csv_count": checked_csv,
        "delisted_member_count": delisted_members,
        "unique_delist_date_count": unique_delist_dates,
        "calendar_end": calendar_end,
        "missing_feature_count": missing_features,
        "membership_records_sha256": actual_records_hash,
        "factor_data_fingerprint": factor_fingerprint,
        "benchmark_fingerprint": str(benchmark_metadata.get("fingerprint") or ""),
        "errors": errors,
        "warnings": warnings,
    }
    if strict and errors:
        preview = "; ".join(errors[:20])
        suffix = f"; ... and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise ValueError(f"PIT dataset validation failed: {preview}{suffix}")
    return report
