"""Live quote projection, rolling bars and non-blocking SQLite recording."""

from __future__ import annotations

import queue
import re
import sqlite3
import threading
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from alphapilot.systems.live.bars import Bar, BarAggregator, DAY_INTERVAL
from alphapilot.systems.live.config import MarketDataConfig
from alphapilot.systems.live.state_io import atomic_write_json
from alphapilot.systems.live.types import Exchange, TickData, normalize_symbol

BAR_INTERVALS = (60, 300)
_DB_RE = re.compile(r"^ticks-(\d{8})\.sqlite3$")


def market_snapshot_path(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser() / "market_snapshot.json"


def read_market_snapshot(state_dir: str | Path) -> dict[str, Any]:
    path = market_snapshot_path(state_dir)
    if not path.exists():
        return {"exists": False, "path": str(path), "ticks": [], "current_bars": {}}
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - status endpoints remain available
        return {"exists": True, "path": str(path), "ticks": [], "current_bars": {}, "error": str(exc)}
    data["exists"] = True
    data["path"] = str(path)
    return data


def refresh_snapshot_ages(
    snapshot: dict[str, Any],
    *,
    symbols: list[str] | None = None,
    stale_after_seconds: float = 3.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recompute quote ages at API read time and optionally filter symbols."""
    data = dict(snapshot)
    reference = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    selected = set(_canonical_symbols(symbols or []))
    ticks: list[dict[str, Any]] = []
    for raw in data.get("ticks") or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if selected and row.get("key") not in selected:
            continue
        received = _parse_datetime(row.get("received_at"))
        age = None
        if received is not None:
            age = max((reference - _coerce_for_subtraction(received, reference)).total_seconds(), 0.0)
        row["age_seconds"] = age
        row["stale"] = age is None or (
            float(stale_after_seconds) > 0 and age > float(stale_after_seconds)
        )
        ticks.append(row)
    data["ticks"] = ticks
    data["stale_after_seconds"] = float(stale_after_seconds)
    return data


def tick_to_dict(tick: TickData, *, now: datetime | None = None) -> dict[str, Any]:
    received_at = tick.received_at
    age_seconds = None
    if received_at is not None:
        reference = now or _now_like(received_at)
        age_seconds = max((reference - _coerce_for_subtraction(received_at, reference)).total_seconds(), 0.0)
    change = tick.last_price - tick.pre_close if tick.last_price and tick.pre_close else 0.0
    change_pct = change / tick.pre_close * 100.0 if tick.pre_close else 0.0
    return {
        "code": tick.code,
        "exchange": tick.exchange.value if isinstance(tick.exchange, Exchange) else str(tick.exchange),
        "key": tick.key,
        "name": tick.name,
        "last_price": tick.last_price,
        "pre_close": tick.pre_close,
        "open_price": tick.open_price,
        "high_price": tick.high_price,
        "low_price": tick.low_price,
        "limit_up": tick.limit_up,
        "limit_down": tick.limit_down,
        "bid_price_1": tick.bid_price_1,
        "ask_price_1": tick.ask_price_1,
        "bid_volume_1": tick.bid_volume_1,
        "ask_volume_1": tick.ask_volume_1,
        "volume": tick.volume,
        "turnover": tick.turnover,
        "gateway": tick.gateway,
        "datetime": _iso(tick.datetime),
        "received_at": _iso(received_at),
        "trading_day": tick.trading_day,
        "age_seconds": age_seconds,
        "change": change,
        "change_pct": change_pct,
    }


def bar_to_dict(bar: Bar, *, interval: int, complete: bool) -> dict[str, Any]:
    return {
        "date": bar.datetime.isoformat(),
        "instrument": bar.instrument,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "amount": bar.amount,
        "interval": int(interval),
        "complete": bool(complete),
    }


class SQLiteTickRecorder:
    """Batch normalized ticks and bars onto a dedicated SQLite writer thread."""

    def __init__(
        self,
        config: MarketDataConfig,
        provider: str,
        *,
        timezone: str = "Asia/Shanghai",
        now_fn: Callable[[], datetime] | None = None,
        connect_fn: Callable[..., sqlite3.Connection] = sqlite3.connect,
    ) -> None:
        self.config = config
        self.provider = str(provider or "quote")
        self.timezone = ZoneInfo(timezone)
        self._now = now_fn or (lambda: datetime.now(self.timezone))
        self._connect_fn = connect_fn
        self._queue: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue(
            maxsize=max(int(config.queue_size), 1)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._connections: dict[str, sqlite3.Connection] = {}
        self._status: dict[str, Any] = {
            "enabled": bool(config.enabled),
            "healthy": True,
            "degraded": False,
            "provider": self.provider,
            "queue_depth": 0,
            "written_ticks": 0,
            "written_bars": 0,
            "dropped_ticks": 0,
            "dropped_bars": 0,
            "last_error": None,
            "last_flush_at": None,
            "active_path": None,
        }

    @property
    def provider_dir(self) -> Path:
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.provider).strip("._") or "quote"
        return Path(self.config.data_dir).expanduser() / slug

    def start(self) -> None:
        if not self.config.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self.provider_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_retention()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"market-recorder-{self.provider}", daemon=True)
        self._thread.start()

    def record_tick(self, tick: TickData) -> None:
        if not self.config.enabled:
            return
        if tick.received_at is None:
            tick.received_at = self._now()
        day = _tick_day(tick, self.timezone, self._now())
        self._put("tick", day, tick_to_dict(tick))

    def record_bar(self, bar: Bar, interval: int, *, complete: bool) -> None:
        if not self.config.enabled:
            return
        day = bar.datetime.astimezone(self.timezone).strftime("%Y%m%d") if bar.datetime.tzinfo else bar.datetime.strftime("%Y%m%d")
        self._put("bar", day, bar_to_dict(bar, interval=interval, complete=complete))

    def _put(self, kind: str, day: str, payload: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((kind, day, payload))
        except queue.Full:
            with self._lock:
                key = "dropped_ticks" if kind == "tick" else "dropped_bars"
                self._status[key] += 1
                self._status["healthy"] = False
                self._status["degraded"] = True
                self._status["last_error"] = "market recorder queue is full"

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        self._stop.set()
        thread = self._thread
        thread.join(timeout=max(float(timeout), 0.0))
        if thread.is_alive():
            self._mark_error("market recorder did not drain before shutdown")
        else:
            self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {**self._status, "queue_depth": self._queue.qsize()}

    def cleanup_retention(self) -> None:
        days = int(self.config.retention_days)
        if days <= 0 or not self.provider_dir.exists():
            return
        cutoff = self._now().date() - timedelta(days=days)
        for path in self.provider_dir.glob("ticks-*.sqlite3"):
            match = _DB_RE.match(path.name)
            if not match:
                continue
            try:
                file_day = datetime.strptime(match.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if file_day < cutoff:
                for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                    candidate.unlink(missing_ok=True)

    def _run(self) -> None:
        try:
            while not self._stop.is_set() or not self._queue.empty():
                batch: list[tuple[str, str, dict[str, Any]]] = []
                try:
                    batch.append(self._queue.get(timeout=max(float(self.config.flush_interval), 0.05)))
                except queue.Empty:
                    continue
                while len(batch) < max(int(self.config.batch_size), 1):
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                try:
                    self._write_batch(batch)
                except Exception as exc:  # noqa: BLE001 - recording must not stop trading
                    tick_count = sum(item[0] == "tick" for item in batch)
                    bar_count = len(batch) - tick_count
                    with self._lock:
                        self._status["dropped_ticks"] += tick_count
                        self._status["dropped_bars"] += bar_count
                    self._mark_error(f"{type(exc).__name__}: {exc}")
                    self._close_connections()
                finally:
                    for _item in batch:
                        self._queue.task_done()
        finally:
            self._close_connections()

    def _write_batch(self, batch: list[tuple[str, str, dict[str, Any]]]) -> None:
        by_day: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for kind, day, payload in batch:
            by_day[day].append((kind, payload))
        written_ticks = 0
        written_bars = 0
        for day, items in by_day.items():
            connection = self._connection(day)
            ticks = [payload for kind, payload in items if kind == "tick"]
            bars = [payload for kind, payload in items if kind == "bar"]
            with connection:
                if ticks:
                    connection.executemany(_TICK_INSERT, [_tick_values(row) for row in ticks])
                    written_ticks += len(ticks)
                if bars:
                    connection.executemany(_BAR_UPSERT, [_bar_values(row) for row in bars])
                    written_bars += len(bars)
        with self._lock:
            self._status["written_ticks"] += written_ticks
            self._status["written_bars"] += written_bars
            self._status["last_flush_at"] = self._now().isoformat()
            if not self._status.get("last_error"):
                self._status["healthy"] = True

    def _connection(self, day: str) -> sqlite3.Connection:
        connection = self._connections.get(day)
        if connection is not None:
            return connection
        path = self.provider_dir / f"ticks-{day}.sqlite3"
        connection = self._connect_fn(str(path), timeout=10.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.executescript(_SCHEMA)
        self._connections[day] = connection
        with self._lock:
            self._status["active_path"] = str(path)
        return connection

    def _close_connections(self) -> None:
        for connection in self._connections.values():
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass
        self._connections.clear()

    def _mark_error(self, error: str) -> None:
        with self._lock:
            self._status["healthy"] = False
            self._status["degraded"] = True
            self._status["last_error"] = error


class LiveMarketDataService:
    """Own the latest quote projection, live bars and optional recorder."""

    def __init__(
        self,
        config: MarketDataConfig,
        provider: str,
        symbols: list[str],
        *,
        state_dir: str | Path,
        timezone: str = "Asia/Shanghai",
        now_fn: Callable[[], datetime] | None = None,
        recording: bool | None = None,
    ) -> None:
        self.config = config
        self.provider = str(provider or "quote")
        self.state_dir = Path(state_dir).expanduser()
        self.timezone = ZoneInfo(timezone)
        self._now = now_fn or (lambda: datetime.now(self.timezone))
        self.symbols = _canonical_symbols(symbols)
        recorder_config = MarketDataConfig(**asdict(config))
        if recording is not None:
            recorder_config.enabled = bool(recording)
        self.recorder = SQLiteTickRecorder(
            recorder_config,
            self.provider,
            timezone=timezone,
            now_fn=self._now,
        )
        self._lock = threading.RLock()
        self._latest: dict[str, TickData] = {}
        self._bars = {
            interval: BarAggregator(interval, on_bar=lambda bar, i=interval: self._on_bar(i, bar))
            for interval in (*BAR_INTERVALS, DAY_INTERVAL)
        }
        self._bar_listeners: dict[int, list[Callable[[Bar], None]]] = {}
        self._engine: Any = None
        self._last_session: str | None = None

    def start(self, engine: Any) -> None:
        self._engine = engine
        self.recorder.start()
        engine.add_tick_listener(self.on_tick)

    def on_tick(self, tick: TickData) -> None:
        """Fast callback path: update memory, aggregate and enqueue without blocking."""
        try:
            if tick.received_at is None:
                tick.received_at = self._now()
            with self._lock:
                self._latest[tick.key] = tick
                for aggregator in self._bars.values():
                    aggregator.on_tick(tick)
            self.recorder.record_tick(tick)
        except Exception as exc:  # noqa: BLE001 - observability cannot break trading
            self.recorder._mark_error(f"market projection error: {type(exc).__name__}: {exc}")

    def add_bar_listener(self, interval: int, listener: Callable[[Bar], None]) -> None:
        if int(interval) not in self._bars:
            raise ValueError(f"unsupported live bar interval {interval}")
        self._bar_listeners.setdefault(int(interval), []).append(listener)

    def remove_bar_listener(self, interval: int, listener: Callable[[Bar], None]) -> None:
        listeners = self._bar_listeners.get(int(interval), [])
        if listener in listeners:
            listeners.remove(listener)

    def step(self, session: Any) -> None:
        value = str(getattr(session, "value", session))
        if value != self._last_session and value in {"lunch_break", "post_close", "closed"}:
            self.flush_bars()
        self._last_session = value

    def flush_bars(self) -> None:
        with self._lock:
            for aggregator in self._bars.values():
                aggregator.flush()

    def snapshot(self) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            ticks = [tick_to_dict(tick, now=now) for tick in self._latest.values()]
            current_bars = {
                str(interval): {
                    bar.instrument: bar_to_dict(bar, interval=interval, complete=False)
                    for bar in aggregator.current()
                }
                for interval, aggregator in self._bars.items()
            }
        ticks.sort(key=lambda row: row["key"])
        stale_after = max(float(self.config.stale_after_seconds), 0.0)
        for row in ticks:
            age = row.get("age_seconds")
            row["stale"] = age is None or (stale_after > 0 and float(age) > stale_after)
        return {
            "exists": True,
            "generated_at": now.isoformat(),
            "quote_provider": self.provider,
            "subscribed_symbols": list(self.symbols),
            "stale_after_seconds": stale_after,
            "ticks": ticks,
            "current_bars": current_bars,
            "recorder": self.recorder.status(),
        }

    def write_snapshot(self) -> dict[str, Any]:
        data = self.snapshot()
        atomic_write_json(market_snapshot_path(self.state_dir), data)
        return data

    def close(self, timeout: float = 5.0) -> dict[str, Any]:
        with self._lock:
            for interval, aggregator in self._bars.items():
                for bar in aggregator.current():
                    self.recorder.record_bar(bar, interval, complete=False)
        if self._engine is not None and hasattr(self._engine, "remove_tick_listener"):
            self._engine.remove_tick_listener(self.on_tick)
        self.recorder.stop(timeout=timeout)
        return self.write_snapshot()

    def _on_bar(self, interval: int, bar: Bar) -> None:
        self.recorder.record_bar(bar, interval, complete=True)
        for listener in list(self._bar_listeners.get(interval, [])):
            listener(bar)


def load_market_bars(
    data_dir: str | Path,
    provider: str,
    symbol: str,
    interval: int,
    *,
    limit: int = 300,
    current: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Read recent persisted bars and merge a daemon's current partial bar."""
    if int(interval) not in (*BAR_INTERVALS, DAY_INTERVAL):
        raise ValueError(f"interval must be one of {(*BAR_INTERVALS, DAY_INTERVAL)}")
    code, exchange = normalize_symbol(symbol)
    key = f"{code}.{exchange.value}"
    provider_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(provider)).strip("._") or "quote"
    root = Path(data_dir).expanduser() / provider_slug
    wanted = max(1, min(int(limit), 2000))
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("ticks-*.sqlite3"), reverse=True):
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            connection.row_factory = sqlite3.Row
            found = connection.execute(
                "SELECT start_time AS date, symbol AS instrument, open, high, low, close, "
                "volume, amount, interval_seconds AS interval, complete FROM bars "
                "WHERE symbol=? AND interval_seconds=? ORDER BY start_time DESC LIMIT ?",
                (key, int(interval), wanted - len(rows)),
            ).fetchall()
            rows.extend(dict(row) for row in found)
            connection.close()
        except sqlite3.Error:
            continue
        if len(rows) >= wanted:
            break
    rows.sort(key=lambda row: row["date"])
    partial = (current or {}).get(str(interval), {}).get(key)
    if isinstance(partial, dict):
        if rows and rows[-1].get("date") == partial.get("date"):
            rows[-1] = _merge_bar_rows(rows[-1], partial)
        else:
            rows.append(dict(partial))
    return rows[-wanted:]


def _merge_bar_rows(existing: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    return {
        **existing,
        "high": max(float(existing["high"]), float(current["high"])),
        "low": min(float(existing["low"]), float(current["low"])),
        "close": float(current["close"]),
        "volume": float(existing.get("volume") or 0.0) + float(current.get("volume") or 0.0),
        "amount": float(existing.get("amount") or 0.0) + float(current.get("amount") or 0.0),
        "complete": False,
    }


def _canonical_symbols(symbols: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        code, exchange = normalize_symbol(raw)
        key = f"{code}.{exchange.value}"
        if key not in seen:
            seen.add(key)
            output.append(key)
    return output


def _tick_day(tick: TickData, timezone: ZoneInfo, fallback: datetime) -> str:
    raw = str(tick.trading_day or "").replace("-", "")
    if len(raw) == 8 and raw.isdigit():
        return raw
    moment = tick.datetime or tick.received_at or fallback
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone)
    return moment.strftime("%Y%m%d")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _now_like(value: datetime) -> datetime:
    return datetime.now(value.tzinfo) if value.tzinfo else datetime.now()


def _coerce_for_subtraction(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value


def _tick_values(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(column) for column in _TICK_COLUMNS)


def _bar_values(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(row["interval"]), row["instrument"], row["date"], row["open"], row["high"],
        row["low"], row["close"], row.get("volume", 0.0), row.get("amount", 0.0),
        int(bool(row.get("complete"))),
    )


_TICK_COLUMNS = (
    "datetime", "received_at", "trading_day", "key", "code", "exchange", "name", "gateway",
    "last_price", "pre_close", "open_price", "high_price", "low_price", "limit_up", "limit_down",
    "volume", "turnover", "bid_price_1", "ask_price_1", "bid_volume_1", "ask_volume_1",
)
_TICK_INSERT = (
    f"INSERT INTO ticks ({', '.join(_TICK_COLUMNS)}) VALUES "
    f"({', '.join('?' for _ in _TICK_COLUMNS)})"
)
_BAR_UPSERT = """
INSERT INTO bars (
    interval_seconds, symbol, start_time, open, high, low, close, volume, amount, complete
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(interval_seconds, symbol, start_time) DO UPDATE SET
    high=MAX(bars.high, excluded.high),
    low=MIN(bars.low, excluded.low),
    close=excluded.close,
    volume=bars.volume + excluded.volume,
    amount=bars.amount + excluded.amount,
    complete=MAX(bars.complete, excluded.complete)
"""
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    datetime TEXT,
    received_at TEXT NOT NULL,
    trading_day TEXT,
    key TEXT NOT NULL,
    code TEXT NOT NULL,
    exchange TEXT NOT NULL,
    name TEXT,
    gateway TEXT,
    last_price REAL,
    pre_close REAL,
    open_price REAL,
    high_price REAL,
    low_price REAL,
    limit_up REAL,
    limit_down REAL,
    volume REAL,
    turnover REAL,
    bid_price_1 REAL,
    ask_price_1 REAL,
    bid_volume_1 REAL,
    ask_volume_1 REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_key_time ON ticks(key, datetime, id);
CREATE TABLE IF NOT EXISTS bars (
    interval_seconds INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    start_time TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0,
    amount REAL NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(interval_seconds, symbol, start_time)
);
"""
