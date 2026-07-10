"""LiveTimingRunner — drive a timing strategy against the live engine.

Closes the loop the timing system never had: live ticks → bars → strategy →
:class:`OrderIntent` → ``orders_from_intents`` → ``engine.submit``. Everything
here reuses existing seams — the engine's tick listener, the executor's intent
translation, the session clock, and the call-auction algo — so the runner is
mostly wiring, not logic.

Two driving modes (mirroring how the strategies were backtested):

* ``freq="min"`` — ticks aggregate into N-second bars; each completed bar is
  fed to the strategy and any resulting intents are submitted immediately
  (continuous session, risk-gated).
* ``freq="day"`` — ticks aggregate into one daily bar; at ``POST_CLOSE`` the
  bar closes, signals are computed, and the resulting requests are armed as a
  :class:`CallAuctionAlgo` plan for the **next** morning's opening auction —
  matching the backtest's next-bar-open ``shift(1)`` semantics.

The runner is passive: like the algos, an outer loop calls :meth:`step` (a
few times per minute is plenty; e.g. ``EventDispatcher.add_periodic``). Ticks
arrive via the engine's tick listener independently of stepping.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from alphapilot.systems.live.algos import AlgoState, CallAuctionAlgo
from alphapilot.systems.live.bars import Bar, BarAggregator, DAY_INTERVAL
from alphapilot.systems.live.executor import orders_from_intents
from alphapilot.systems.live.fsm.session_fsm import SessionState
from alphapilot.systems.live.types import OrderRequest, TickData


class BarStrategy(Protocol):
    """What the runner needs from a strategy adapter (see BatchStrategyAdapter)."""

    def on_bar(self, bar: Bar) -> list[Any]:
        """Return OrderIntent-shaped objects for one completed bar."""


class LiveTimingRunner:
    """Wire ticks → bars → strategy intents → guarded order submission."""

    def __init__(
        self,
        engine: Any,
        strategy: BarStrategy,
        symbols: list[str],
        *,
        freq: str = "day",
        bar_seconds: int = 60,
        lot_size: int = 100,
        auction_window: str = "open",
    ) -> None:
        if freq not in ("day", "min"):
            raise ValueError(f"freq must be 'day' or 'min', got {freq!r}")
        self.engine = engine
        self.strategy = strategy
        self.symbols = list(symbols)
        self.freq = freq
        self.lot_size = int(lot_size)
        self.auction_window = auction_window

        interval = DAY_INTERVAL if freq == "day" else int(bar_seconds)
        self.bars = BarAggregator(interval, on_bar=self._on_bar_closed)

        self.pending_requests: list[OrderRequest] = []
        self.algo: CallAuctionAlgo | None = None
        self._last_session_state: SessionState | None = None
        self._finalized_day: date | None = None
        self._started = False
        self._paused = False
        self._stopped = False

    # ---- lifecycle ---------------------------------------------------------- #
    def start(self) -> None:
        """Attach to the engine's tick stream and subscribe market data."""
        if self._started and not self._stopped:
            return
        self._stopped = False
        self._paused = False
        self.engine.add_tick_listener(self.on_tick)
        self.engine.subscribe_market_data(self.symbols)
        self._started = True

    def pause(self) -> dict[str, Any]:
        """Pause strategy signal generation without disconnecting market data."""
        if not self._stopped:
            self._paused = True
        return self.status()

    def resume(self) -> dict[str, Any]:
        """Resume a paused runner."""
        if self._started and not self._stopped:
            self._paused = False
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Stop strategy actions for the lifetime of this runner instance."""
        self._paused = False
        self._stopped = True
        self.pending_requests = []
        self.algo = None
        return self.status()

    def status(self) -> dict[str, Any]:
        """Return a compact lifecycle/status projection."""
        return {
            "started": self._started,
            "paused": self._paused,
            "stopped": self._stopped,
            "active": self._started and not self._paused and not self._stopped,
            "symbols": list(self.symbols),
            "freq": self.freq,
            "pending_requests": len(self.pending_requests),
            "algo_armed": self.algo is not None,
            "last_session": None if self._last_session_state is None else self._last_session_state.value,
        }

    # ---- event inputs -------------------------------------------------------- #
    def on_tick(self, tick: TickData) -> None:
        if self._paused or self._stopped:
            return
        self.bars.on_tick(tick)

    def step(self) -> dict[str, Any]:
        """Advance time-driven work; call periodically from the owner loop."""
        if self._stopped:
            status = self.status()
            status["session"] = None
            return status

        state = self.engine.session.tick()
        if self._paused:
            status = self.status()
            status["session"] = state.value
            return status

        # Close partial bars at session boundaries so the last bar of a half-day
        # doesn't wait for a tick that will never come.
        if state != self._last_session_state:
            if state in (SessionState.LUNCH_BREAK, SessionState.POST_CLOSE) and self.freq == "min":
                self.bars.flush()
            if state == SessionState.POST_CLOSE:
                self._finalize_day()
            self._last_session_state = state

        if self.algo is not None:
            if self.algo.step() == AlgoState.DONE:
                self.algo = None

        return {
            **self.status(),
            "session": state.value,
        }

    # ---- internals ------------------------------------------------------------ #
    def _on_bar_closed(self, bar: Bar) -> None:
        if self._paused or self._stopped:
            return
        intents = self.strategy.on_bar(bar)
        if not intents:
            return
        prices = {bar.instrument: bar.close}
        requests = orders_from_intents(
            intents, self.engine.oms, prices, lot_size=self.lot_size
        )
        if not requests:
            return
        if self.freq == "min":
            for req in requests:
                self.engine.submit(req)
        else:
            # Day mode: queue for the next opening auction (backtest shift(1)).
            self.pending_requests.extend(requests)

    def _finalize_day(self) -> None:
        today = self.engine.session._now_fn().date() if hasattr(self.engine.session, "_now_fn") else None
        if today is not None and today == self._finalized_day:
            return
        self._finalized_day = today

        if self.freq == "day":
            self.bars.flush()  # closes the daily bar -> _on_bar_closed -> pending
        if self.pending_requests and self.algo is None:
            self.algo = CallAuctionAlgo(
                self.engine, self.pending_requests, window=self.auction_window
            )
            self.pending_requests = []
