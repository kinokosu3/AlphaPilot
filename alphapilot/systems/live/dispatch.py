"""EventDispatcher — serialize broker-SDK callbacks onto one thread.

Real broker SDKs (XTP / EMT) invoke their Python callbacks from *their own* C++
threads (one for trading, one for quotes). The live stack above the gateway —
OMS, PositionBook, Ledger, risk counters — is deliberately lock-free and relies
on every mutation happening on a single thread (with paper/sim brokers that is
simply the caller thread). This dispatcher restores that guarantee for real
gateways: callbacks are enqueued from any thread and drained by one worker, in
arrival order. It replaces the role vn.py's ``EventEngine`` queue played, minus
the string event types — we enqueue plain callables instead.

It also owns a timer: ``add_periodic(interval, fn)`` registers a task executed
on the *same* worker thread (so periodic account/position polling never races
the callback stream).
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from queue import Empty, Queue


class EventDispatcher:
    """Single-threaded executor for gateway callbacks + periodic tasks."""

    def __init__(self, *, timer_interval: float = 1.0, name: str = "live-dispatch") -> None:
        self._queue: Queue[Callable[[], None]] = Queue()
        self._timer_interval = timer_interval
        self._name = name
        self._active = False
        self._worker: threading.Thread | None = None
        self._timer: threading.Thread | None = None
        # (interval_seconds, elapsed_accumulator, fn) triples, mutated only on the timer thread.
        self._periodic: list[list] = []
        self._error_handler: Callable[[BaseException], None] | None = None

    # ---- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._worker = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._worker.start()
        self._timer = threading.Thread(target=self._run_timer, name=f"{self._name}-timer", daemon=True)
        self._timer.start()

    def stop(self, timeout: float = 2.0) -> None:
        if not self._active:
            return
        self._active = False
        if self._timer is not None:
            self._timer.join(timeout)
            self._timer = None
        if self._worker is not None:
            self._worker.join(timeout)
            self._worker = None

    @property
    def active(self) -> bool:
        return self._active

    # ---- submission ------------------------------------------------------- #
    def put(self, fn: Callable[[], None]) -> None:
        """Enqueue ``fn`` to run on the dispatch thread (callable from any thread)."""
        self._queue.put(fn)

    def add_periodic(self, interval: float, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the dispatch thread every ``interval`` seconds."""
        self._periodic.append([float(interval), 0.0, fn])

    def set_error_handler(self, handler: Callable[[BaseException], None]) -> None:
        """Install a hook for exceptions raised by dispatched callables."""
        self._error_handler = handler

    # ---- draining helpers (tests / synchronous mode) ----------------------- #
    def drain(self, timeout: float = 5.0) -> None:
        """Block until the queue is empty (best effort; for tests and shutdown)."""
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.01)

    def run_pending(self) -> int:
        """Synchronously drain the queue on the *calling* thread.

        Only for use when the dispatcher is not started (offline tests): gives
        deterministic, single-threaded delivery without any worker thread.
        """
        count = 0
        while True:
            try:
                fn = self._queue.get_nowait()
            except Empty:
                return count
            self._invoke(fn)
            count += 1

    # ---- internals --------------------------------------------------------- #
    def _run(self) -> None:
        while self._active:
            try:
                fn = self._queue.get(timeout=1.0)
            except Empty:
                continue
            self._invoke(fn)

    def _run_timer(self) -> None:
        # Tick in small steps so stop() is responsive; fire tasks whose
        # accumulated elapsed time crossed their interval.
        step = min(self._timer_interval, 0.2)
        while self._active:
            time.sleep(step)
            for task in self._periodic:
                task[1] += step
                if task[1] >= task[0]:
                    task[1] = 0.0
                    self.put(task[2])

    def _invoke(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a broken handler must not kill the loop
            if self._error_handler is not None:
                self._error_handler(exc)
            else:
                traceback.print_exc()
