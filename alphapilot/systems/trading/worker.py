"""Crash-isolated batch strategy execution for trusted third-party code."""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path
import queue
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
        process.join(self.timeout)
        if process.is_alive():
            process.terminate()
            process.join(2.0)
            raise TimeoutError(f"strategy evaluation exceeded {self.timeout:.1f}s")
        try:
            ok, value = output.get(timeout=1.0)
        except queue.Empty as exc:
            raise RuntimeError(f"strategy worker exited with code {process.exitcode}") from exc
        if not ok:
            raise RuntimeError(value)
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
        try:
            import resource

            limit = int(memory_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
        except (ImportError, OSError, ValueError):
            pass
        from alphapilot.systems.trading.registry import _load_factory

        factory = _load_factory(factory_path, base=Path(base) if base else None)
        strategy = factory(**params)
        result = strategy.generate_signals(bars, context)
        output.put((True, result))
    except BaseException as exc:  # noqa: BLE001 - marshal child failure
        output.put((False, f"{type(exc).__name__}: {exc}"))
