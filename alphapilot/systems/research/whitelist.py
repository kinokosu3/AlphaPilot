"""Liquidity-based, immutable live whitelist construction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd


def _normalise_symbol(value: Any) -> str:
    symbol = str(value or "").replace(".", "").upper()
    for exchange in ("SH", "SZ", "BJ"):
        if symbol.endswith(exchange) and symbol[:-2].isdigit():
            return f"{exchange}{symbol[:-2]}"
    return symbol


def build_live_whitelist(
    bars: pd.DataFrame,
    stock_basic: pd.DataFrame,
    *,
    account_equity: float,
    as_of: str,
    top_n: int = 50,
    liquidity_days: int = 60,
    minimum_trading_age: int = 120,
    lot_size: int = 100,
    max_lot_equity_ratio: float = 0.02,
) -> dict[str, Any]:
    """Select the most liquid eligible names and return a fingerprinted payload."""

    if account_equity <= 0:
        raise ValueError("account_equity must be positive")
    if not isinstance(bars.index, pd.MultiIndex) or bars.index.nlevels < 2:
        raise ValueError("bars must use a (datetime, instrument) MultiIndex")
    required = {"close", "volume", "amount"}
    missing = sorted(required - set(bars.columns))
    if missing:
        raise ValueError(f"bars is missing columns: {', '.join(missing)}")
    frame = bars.reset_index()
    date_column = "datetime" if "datetime" in frame.columns else frame.columns[0]
    instrument_column = (
        "instrument" if "instrument" in frame.columns else frame.columns[1]
    )
    frame[date_column] = pd.to_datetime(frame[date_column], errors="coerce")
    frame[instrument_column] = frame[instrument_column].map(_normalise_symbol)
    cutoff = pd.Timestamp(as_of)
    frame = frame.loc[frame[date_column].notna() & (frame[date_column] <= cutoff)].copy()
    if frame.empty:
        raise ValueError("bars contains no observations on or before as_of")
    sessions = sorted(frame[date_column].drop_duplicates())
    recent_sessions = sessions[-liquidity_days:]
    recent = frame[frame[date_column].isin(recent_sessions)]
    median_amount = recent.groupby(instrument_column)["amount"].median()
    trading_age = frame.groupby(instrument_column)[date_column].nunique()
    latest_date = frame[date_column].max()
    latest = (
        frame.loc[frame[date_column] == latest_date]
        .sort_values(instrument_column)
        .drop_duplicates(instrument_column, keep="last")
        .set_index(instrument_column)
    )
    basic = stock_basic.copy()
    symbol_column = next(
        (column for column in ("symbol", "ts_code", "instrument", "code") if column in basic.columns),
        None,
    )
    if symbol_column is None:
        raise ValueError("stock_basic requires symbol/ts_code/instrument/code")
    basic["_symbol"] = basic[symbol_column].map(_normalise_symbol)
    basic = basic.drop_duplicates("_symbol", keep="last").set_index("_symbol")

    rows: list[dict[str, Any]] = []
    lot_limit = float(account_equity) * float(max_lot_equity_ratio)
    for symbol, amount in median_amount.sort_values(ascending=False).items():
        if symbol not in latest.index or symbol not in basic.index:
            continue
        market_row = latest.loc[symbol]
        member = basic.loc[symbol]
        name = str(member.get("name") or "")
        status = str(member.get("list_status") or "L").upper()
        suspended = bool(member.get("suspended", False)) or float(market_row["volume"]) <= 0
        age = int(trading_age.get(symbol, 0))
        close = float(market_row["close"])
        lot_value = close * lot_size
        if "ST" in name.upper() or status != "L" or suspended:
            continue
        if age < minimum_trading_age or lot_value > lot_limit:
            continue
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "median_amount_60d": float(amount),
                "trading_age": age,
                "close": close,
                "lot_size": lot_size,
                "one_lot_value": lot_value,
            }
        )
        if len(rows) >= top_n:
            break
    payload: dict[str, Any] = {
        "schema_version": 1,
        "as_of": str(cutoff.date()),
        "latest_market_date": str(pd.Timestamp(latest_date).date()),
        "account_equity": float(account_equity),
        "selection": {
            "top_n": top_n,
            "liquidity_days": liquidity_days,
            "minimum_trading_age": minimum_trading_age,
            "lot_size": lot_size,
            "max_lot_equity_ratio": max_lot_equity_ratio,
        },
        "symbols": [row["symbol"] for row in rows],
        "records": rows,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["fingerprint"] = hashlib.sha256(canonical).hexdigest()
    return payload


def freeze_whitelist(payload: dict[str, Any], output_path: str | Path) -> Path:
    """Write once; an existing whitelist may never be overwritten."""

    target = Path(output_path).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"refusing to overwrite frozen whitelist: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return target


def verify_whitelist(payload: dict[str, Any]) -> dict[str, Any]:
    """Verify a frozen whitelist's content hash and structural invariants."""

    value = dict(payload or {})
    expected = str(value.pop("fingerprint", ""))
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    actual = hashlib.sha256(canonical).hexdigest()
    symbols = [str(item) for item in value.get("symbols") or []]
    records = list(value.get("records") or [])
    errors: list[str] = []
    if not expected or expected != actual:
        errors.append("whitelist fingerprint is missing or changed")
    if len(symbols) != len(set(symbols)):
        errors.append("whitelist symbols are not unique")
    if [str(item.get("symbol") or "") for item in records] != symbols:
        errors.append("whitelist records do not match symbol order")
    return {
        "ok": not errors,
        "fingerprint": actual,
        "symbols": symbols,
        "errors": errors,
    }
