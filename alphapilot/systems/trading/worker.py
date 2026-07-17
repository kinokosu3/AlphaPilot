"""Crash-isolated batch strategy execution for trusted third-party code."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path
import queue
import threading
from typing import Any


class IsolatedBatchStrategy:
    """Execute each third-party batch evaluation in a bounded child process.

    This isolates crashes and hangs; it is deliberately not described as a
    security sandbox because trusted Python code can still access the host.
    """

    def __init__(
        self,
        factory_path: str,
        params: dict[str, Any],
        *,
        base: str | Path | None = None,
        timeout: float = 10.0,
        memory_mb: int = 1024,
    ) -> None:
        self.factory_path = factory_path
        self.params = dict(params)
        self.base = str(base) if base is not None else None
        self.timeout = max(float(timeout), 0.1)
        self.memory_mb = max(int(memory_mb), 256)
        self.name = factory_path

    def generate_signals(self, bars: Any, context: Any) -> Any:
        method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
        ctx = mp.get_context(method)
        output = ctx.Queue(maxsize=1)
        process = ctx.Process(
            target=_evaluate,
            args=(output, self.factory_path, self.params, self.base, bars, context, self.memory_mb),
            daemon=True,
        )
        process.start()
        try:
            # Drain the queue before joining.  ``multiprocessing.Queue`` uses a
            # feeder thread; joining first can deadlock when a DataFrame result
            # fills the pipe while the parent is waiting for process exit.
            ok, value = output.get(timeout=self.timeout)
        except queue.Empty as exc:
            if process.is_alive():
                process.terminate()
                process.join(2.0)
                raise TimeoutError(
                    f"strategy evaluation exceeded {self.timeout:.1f}s"
                ) from exc
            raise RuntimeError(
                f"strategy worker exited with code {process.exitcode}"
            ) from exc
        process.join(2.0)
        if process.is_alive():
            process.terminate()
            process.join(2.0)
        if not ok:
            raise RuntimeError(value)
        return value


class PersistentStrategyWorker:
    """Keep one trusted provider instance in a bounded worker process."""

    def __init__(
        self,
        factory_path: str,
        params: dict[str, Any],
        *,
        base: str | Path | None = None,
        timeout: float = 10.0,
        memory_mb: int = 1024,
    ) -> None:
        self.factory_path = factory_path
        self.params = dict(params)
        self.base = str(base) if base is not None else None
        self.timeout = max(float(timeout), 0.1)
        self.memory_mb = max(int(memory_mb), 256)
        self._process: Any | None = None
        self._connection: Any | None = None
        self._lock = threading.RLock()

    def initialize(self, context: Any) -> None:
        self._call("initialize", context)

    def warmup(self, history: Any) -> None:
        self._call("warmup", history)

    def evaluate(self, context: Any) -> Any:
        return self._call("evaluate", context)

    def snapshot(self) -> dict[str, Any]:
        return dict(self._call("snapshot") or {})

    def restore(self, state: dict[str, Any]) -> None:
        self._call("restore", state)

    def stop(self, reason: str) -> None:
        if self._process is None:
            return
        try:
            self._call("stop", reason)
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                try:
                    self._connection.send(("__close__", (), {}))
                except (BrokenPipeError, EOFError, OSError):
                    pass
            if self._process is not None:
                self._process.join(1.0)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(2.0)
            self._connection = None
            self._process = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown is best effort
            pass

    def _start(self) -> None:
        method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
        ctx = mp.get_context(method)
        parent, child = ctx.Pipe(duplex=True)
        process = ctx.Process(
            target=_provider_loop,
            args=(
                child, self.factory_path, self.params, self.base, self.memory_mb,
            ),
            daemon=True,
        )
        process.start()
        child.close()
        self._connection = parent
        self._process = process

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            if self._process is None or not self._process.is_alive():
                self.close()
                self._start()
            assert self._connection is not None
            assert self._process is not None
            try:
                self._connection.send((method, args, kwargs))
            except (BrokenPipeError, EOFError, OSError) as exc:
                self.close()
                raise RuntimeError("strategy worker is unavailable") from exc
            if not self._connection.poll(self.timeout):
                self.close()
                raise TimeoutError(f"strategy worker call {method} exceeded {self.timeout:.1f}s")
            try:
                ok, value = self._connection.recv()
            except (EOFError, OSError) as exc:
                code = self._process.exitcode
                self.close()
                raise RuntimeError(f"strategy worker exited with code {code}") from exc
            if not ok:
                raise RuntimeError(str(value))
            return value


def _evaluate(
    output: Any,
    factory_path: str,
    params: dict[str, Any],
    base: str | None,
    bars: Any,
    context: Any,
    memory_mb: int,
) -> None:
    try:
        _apply_limits(memory_mb, cpu_seconds=30)
        from alphapilot.systems.trading.registry import _load_factory

        factory = _load_factory(factory_path, base=Path(base) if base else None)
        strategy = factory(**params)
        result = strategy.generate_signals(bars, context)
        output.put((True, result))
    except BaseException as exc:  # noqa: BLE001 - marshal child failure
        output.put((False, f"{type(exc).__name__}: {exc}"))


def _provider_loop(
    connection: Any,
    factory_path: str,
    params: dict[str, Any],
    base: str | None,
    memory_mb: int,
) -> None:
    try:
        _apply_limits(memory_mb)
        from alphapilot.systems.trading.registry import _load_factory

        factory = _load_factory(factory_path, base=Path(base) if base else None)
        provider = factory(**params)
        while True:
            try:
                method, args, kwargs = connection.recv()
            except EOFError:
                break
            if method == "__close__":
                break
            try:
                result = getattr(provider, method)(*args, **kwargs)
                connection.send((True, result))
            except BaseException as exc:  # noqa: BLE001 - marshal provider failure
                connection.send((False, f"{type(exc).__name__}: {exc}"))
    except BaseException as exc:  # noqa: BLE001
        try:
            connection.send((False, f"{type(exc).__name__}: {exc}"))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _apply_limits(memory_mb: int, *, cpu_seconds: int = 300) -> None:
    try:
        import os
        import resource

        requested = int(memory_mb) * 1024 * 1024
        # pandas/numpy/OpenBLAS can reserve more than 1 GiB of *virtual* address
        # space before strategy code runs.  Setting RLIMIT_AS below the child's
        # existing VMS makes the next harmless allocation hang/fail.  Keep the
        # configured budget as a floor and allow 256 MiB of working headroom
        # above the already mapped scientific runtime.
        current_vms = 0
        try:
            pages = int(Path("/proc/self/statm").read_text().split()[0])
            current_vms = pages * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError, IndexError):
            pass
        limit = max(requested, current_vms + 256 * 1024 * 1024)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    except (ImportError, OSError, ValueError):
        pass
