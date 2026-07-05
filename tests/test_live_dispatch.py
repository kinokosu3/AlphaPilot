"""EventDispatcher: ordering, sync drain, periodic tasks, error isolation."""

from __future__ import annotations

import threading
import time

from alphapilot.systems.live.dispatch import EventDispatcher


def test_run_pending_executes_in_fifo_order() -> None:
    d = EventDispatcher()
    seen: list[int] = []
    for i in range(5):
        d.put(lambda i=i: seen.append(i))
    assert d.run_pending() == 5
    assert seen == [0, 1, 2, 3, 4]


def test_worker_thread_processes_and_serializes() -> None:
    d = EventDispatcher(name="test-dispatch")
    d.start()
    try:
        seen: list[tuple[int, str]] = []

        def make(i: int):
            return lambda: seen.append((i, threading.current_thread().name))

        # enqueue from two producer threads; consumption must be single-threaded
        producers = [
            threading.Thread(target=lambda base=base: [d.put(make(base + i)) for i in range(50)])
            for base in (0, 1000)
        ]
        for p in producers:
            p.start()
        for p in producers:
            p.join()
        d.drain()
        time.sleep(0.05)
        assert len(seen) == 100
        assert {name for _, name in seen} == {"test-dispatch"}
        # per-producer FIFO preserved
        first = [i for i, _ in seen if i < 1000]
        second = [i for i, _ in seen if i >= 1000]
        assert first == sorted(first)
        assert second == sorted(second)
    finally:
        d.stop()


def test_periodic_task_fires_on_dispatch_thread() -> None:
    d = EventDispatcher(timer_interval=0.05, name="tick-dispatch")
    hits: list[str] = []
    d.add_periodic(0.05, lambda: hits.append(threading.current_thread().name))
    d.start()
    try:
        time.sleep(0.5)
    finally:
        d.stop()
    assert len(hits) >= 2
    assert set(hits) == {"tick-dispatch"}


def test_error_handler_isolates_failures() -> None:
    d = EventDispatcher()
    errors: list[str] = []
    d.set_error_handler(lambda exc: errors.append(str(exc)))
    seen: list[int] = []
    d.put(lambda: seen.append(1))
    d.put(lambda: 1 / 0)
    d.put(lambda: seen.append(2))
    d.run_pending()
    assert seen == [1, 2]
    assert len(errors) == 1 and "division" in errors[0]


def test_stop_is_idempotent_and_restartable() -> None:
    d = EventDispatcher()
    d.start()
    d.stop()
    d.stop()
    assert not d.active
