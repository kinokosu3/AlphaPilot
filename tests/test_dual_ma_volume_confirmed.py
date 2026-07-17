from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from alphapilot.systems.timing.base import TimingContext
from alphapilot.systems.trading.registry import StrategyRegistry, resolve_required_history
from alphapilot.systems.trading.worker import IsolatedBatchStrategy


def _bars(*, confirmed_volume: bool) -> pd.DataFrame:
    closes = [10.0] * 55
    closes += [10.0 + index * 0.4 for index in range(1, 21)]
    closes += [18.0 - index * 0.35 for index in range(1, 46)]
    volumes = [100.0] * len(closes)
    if confirmed_volume:
        for index in range(55, 75):
            volumes[index] = 1_000.0
    return pd.DataFrame(
        {
            "datetime": pd.date_range("2025-01-01", periods=len(closes), freq="B"),
            "instrument": "SH600000",
            "open": closes,
            "high": [value + 0.1 for value in closes],
            "low": [value - 0.1 for value in closes],
            "close": closes,
            "volume": volumes,
            "amount": [value * volume for value, volume in zip(closes, volumes)],
        }
    )


def _registry() -> StrategyRegistry:
    root = Path(__file__).resolve().parents[1] / "strategies"
    return StrategyRegistry(local_root=root).discover(builtin_contributions=[])


def test_manifest_registers_day_only_strategy_with_51_bar_warmup() -> None:
    registry = _registry()
    definition = registry.get("dual_ma_volume_confirmed")

    assert definition.required_history == 51
    assert definition.supported_frequencies == ("day",)
    assert resolve_required_history(definition, {
        "short_window": 10,
        "long_window": 50,
        "volume_window": 20,
    }) == 51

    builtin = StrategyRegistry(local_root="/does-not-exist").discover()
    dual_ma = builtin.get("dual_ma")
    assert resolve_required_history(
        dual_ma, {"short_window": 10, "long_window": 50}
    ) == 51
    params = builtin.create(
        "dual_ma", {"short_window": 10, "long_window": 50}, isolated=False
    ).params
    assert params["short_window"] == 10
    assert params["long_window"] == 50


def test_volume_confirmation_controls_entry_and_reverse_cross_exits() -> None:
    registry = _registry()
    strategy = registry.create(
        "dual_ma_volume_confirmed",
        {
            "short_window": 10,
            "long_window": 50,
            "volume_window": 20,
            "target_percent": 0.4,
        },
        isolated=False,
    )

    confirmed = strategy.generate_signals(_bars(confirmed_volume=True), TimingContext())
    unconfirmed = strategy.generate_signals(_bars(confirmed_volume=False), TimingContext())

    assert confirmed["signal"].max() == 1
    first_entry = confirmed.index[confirmed["signal"] == 1][0]
    assert confirmed.loc[first_entry, "target_percent"] == 0.4
    assert confirmed.iloc[-1]["signal"] == 0
    assert unconfirmed["signal"].max() == 0


def test_isolated_worker_is_deterministic() -> None:
    registry = _registry()
    worker = registry.create(
        "dual_ma_volume_confirmed",
        {
            "short_window": 10,
            "long_window": 50,
            "volume_window": 20,
            "target_percent": 0.4,
        },
        isolated=True,
    )
    bars = _bars(confirmed_volume=True)

    first = worker.generate_signals(bars, TimingContext())
    second = worker.generate_signals(bars, TimingContext())

    pd.testing.assert_frame_equal(first, second)


def test_isolated_worker_timeout_is_terminated_and_next_call_recovers(
    tmp_path: Path,
) -> None:
    strategy_dir = tmp_path / "recoverable"
    strategy_dir.mkdir()
    sentinel = tmp_path / "first-call-started"
    (strategy_dir / "provider.py").write_text(
        "from pathlib import Path\n"
        "import time\n"
        "class Recoverable:\n"
        "    def __init__(self, sentinel): self.sentinel = Path(sentinel)\n"
        "    def generate_signals(self, bars, context):\n"
        "        if not self.sentinel.exists():\n"
        "            self.sentinel.write_text('started', encoding='utf-8')\n"
        "            time.sleep(2.0)\n"
        "        return bars.assign(signal=0, score=0.0, reason='recovered')\n",
        encoding="utf-8",
    )
    worker = IsolatedBatchStrategy(
        "provider:Recoverable",
        {"sentinel": str(sentinel)},
        base=strategy_dir,
        timeout=0.2,
    )
    bars = _bars(confirmed_volume=True).head(2)

    with pytest.raises(TimeoutError, match="exceeded"):
        worker.generate_signals(bars, TimingContext())
    assert sentinel.is_file()

    recovered = worker.generate_signals(bars, TimingContext())
    assert recovered["reason"].tolist() == ["recovered", "recovered"]
