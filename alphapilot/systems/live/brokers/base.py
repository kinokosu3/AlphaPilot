"""SdkBrokerGateway — shared plumbing for real broker-SDK gateways (XTP / EMT / …).

A real gateway differs from paper/sim in exactly three ways, and this base class
owns all of them so a concrete broker only supplies mapping tables + callback
converters:

1. **Thread marshalling** — SDK callbacks arrive on vendor C++ threads; every
   ``post_*`` helper enqueues the emit onto the gateway's
   :class:`~alphapilot.systems.live.dispatch.EventDispatcher`, so the OMS (and
   everything above it) keeps its single-writer, lock-free discipline.
2. **Periodic polling** — A-share counters don't push account/position updates;
   ``start()`` installs a round-robin ``query_account`` / ``query_position``
   poll on the dispatch thread (vn.py's 2-second ``EVENT_TIMER`` equivalent).
3. **SDK housekeeping** — vendor log folder (GBK-encodable path) and error
   formatting, identical across brokers.

Adding a broker = subclass this, implement ``connect / close / send_order /
cancel_order / query_account / query_position / subscribe`` with the vendor's
pybind API, and convert vendor dicts to :mod:`alphapilot.systems.live.types`
in the callbacks — see ``xtp_pro.py`` for the reference implementation.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from alphapilot.systems.live.dispatch import EventDispatcher
from alphapilot.systems.live.gateway import BrokerGateway
from alphapilot.systems.live.types import (
    Account,
    Contract,
    Order,
    Position,
    TickData,
    Trade,
)


def round_to(value: float, target: float) -> float:
    """Round ``value`` to the nearest multiple of ``target`` (vn.py's round_to)."""
    if target <= 0:
        return float(value)
    decimal_value = Decimal(str(value))
    decimal_target = Decimal(str(target))
    return float(int(round(decimal_value / decimal_target)) * decimal_target)


def sdk_log_path(broker_name: str) -> Path:
    """Folder handed to vendor SDKs for their own log files.

    Override with ``ALPHAPILOT_SDK_LOG_DIR``. The path must survive
    ``str(path).encode("GBK")`` — both XTP and EMT C++ APIs take a GBK-encoded
    ``save_file_path`` — so keep it ASCII unless you know the locale is set up.
    """
    root = os.environ.get("ALPHAPILOT_SDK_LOG_DIR", "")
    base = Path(root) if root else Path.home() / ".alphapilot" / "sdk_logs"
    path = base / broker_name.lower()
    path.mkdir(parents=True, exist_ok=True)
    return path


class SdkBrokerGateway(BrokerGateway):
    """Base class for gateways that wrap a vendor SDK (see module docstring)."""

    #: seconds between polls; each poll fires ONE of account/position (round-robin),
    #: matching the classic vn.py cadence (full refresh every ``2 * poll_interval``).
    poll_interval: float = 2.0

    def __init__(self, name: str | None = None, *, dispatcher: EventDispatcher | None = None) -> None:
        super().__init__(name)
        self.dispatcher: EventDispatcher = dispatcher or EventDispatcher(name=f"{self.name}-dispatch")
        self._polling_installed = False

    # ---- lifecycle --------------------------------------------------------- #
    def start(self, *, polling: bool = True) -> None:
        """Start the dispatch thread (call at the top of ``connect``)."""
        self.dispatcher.set_error_handler(self._on_dispatch_error)
        self.dispatcher.start()
        if polling and not self._polling_installed:
            self._install_polling()
            self._polling_installed = True

    def shutdown(self) -> None:
        """Stop the dispatch thread (call at the end of ``close``)."""
        self.dispatcher.drain()
        self.dispatcher.stop()

    # ---- thread-safe emit helpers (callable from any SDK thread) ----------- #
    def post(self, fn: Callable, *args) -> None:
        """Run ``fn(*args)`` on the dispatch thread."""
        self.dispatcher.put(lambda: fn(*args))

    def post_tick(self, tick: TickData) -> None:
        self.post(self._emit_tick, tick)

    def post_order(self, order: Order) -> None:
        self.post(self._emit_order, order)

    def post_trade(self, trade: Trade) -> None:
        self.post(self._emit_trade, trade)

    def post_position(self, position: Position) -> None:
        self.post(self._emit_position, position)

    def post_account(self, account: Account) -> None:
        self.post(self._emit_account, account)

    def post_contract(self, contract: Contract) -> None:
        self.post(self._emit_contract, contract)

    def post_log(self, msg: str, level: str = "info") -> None:
        self.post(self._emit_log, msg, level)

    def post_error(self, msg: str, error: dict | None) -> None:
        """Format a vendor ``{error_id, error_msg}`` dict into an error log line."""
        if error:
            msg = f"{msg}，代码：{error.get('error_id')}，信息：{error.get('error_msg')}"
        self.post_log(msg, "error")

    def post_gateway_connected(self, channel: str, detail: str = "") -> None:
        self.post(self._emit_gateway_connected, str(channel), str(detail))

    def post_gateway_disconnected(self, channel: str, reason: str = "", *, halt: bool = True) -> None:
        self.dispatcher.put(lambda: self._emit_gateway_disconnected(str(channel), str(reason), halt=halt))

    # ---- internals ---------------------------------------------------------- #
    def _install_polling(self) -> None:
        tasks = [self.query_account, self.query_position]
        state = {"i": 0}

        def poll() -> None:
            fn = tasks[state["i"] % len(tasks)]
            state["i"] += 1
            fn()

        self.dispatcher.add_periodic(self.poll_interval, poll)

    def _on_dispatch_error(self, exc: BaseException) -> None:
        # Emit directly: we are already on the dispatch thread here.
        self._emit_log(f"dispatch handler error: {type(exc).__name__}: {exc}", level="error")
