#!/usr/bin/env python3
"""Fail unless the 0.2.0 public compatibility entrypoints are absent."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LEGACY_CLI = {
    "timing_strategies",
    "timing_signal",
    "timing_backtest",
    "live_daemon_strategy_status",
    "live_daemon_strategy_start",
    "live_daemon_strategy_pause",
    "live_daemon_strategy_resume",
    "live_daemon_strategy_stop",
}
LEGACY_EXACT_PATHS = {
    "/api/trading/strategy-instances/{instance_id}/backtest",
    "/api/trading/deployments/{instance_id}/{action}",
    "/api/trading/stage-runs/{instance_id}/{stage}/start",
    "/api/trading/stage-runs/{run_id}/finish",
    "/api/trading/stage-runs/{instance_id}/{stage}/evaluate",
}
LEGACY_PATH_PREFIXES = (
    "/api/timing/",
    "/api/live/daemon/strategy/",
)
PORTAL_FORBIDDEN_TEXT = (
    "/api/timing/",
    "/api/live/daemon/strategy/",
    'kind: "timing_backtest"',
    "kind: 'timing_backtest'",
)
DAEMON_FORBIDDEN_TEXT = (
    "--timing-strategy",
    "timing_strategy:",
    'payload.get("timing_strategy")',
)


def main() -> int:
    from alphapilot.kernel import build_engine
    from alphapilot.modules.portal.api import create_app

    engine = build_engine()
    schema = create_app(engine=engine).openapi()
    paths = set(schema.get("paths") or {})
    failures: list[str] = []
    for path in sorted(paths):
        if path in LEGACY_EXACT_PATHS or path.startswith(LEGACY_PATH_PREFIXES):
            failures.append(f"openapi:{path}")

    commands = set(engine.collect_commands())
    failures.extend(f"cli:{name}" for name in sorted(commands & LEGACY_CLI))

    portal_root = ROOT / "alphapilot" / "modules" / "portal" / "web" / "src"
    for path in sorted(portal_root.rglob("*")):
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx"} or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        if any(marker in content for marker in PORTAL_FORBIDDEN_TEXT):
            failures.append(f"portal:{path.relative_to(ROOT)}")

    daemon_path = ROOT / "alphapilot" / "systems" / "live" / "daemon.py"
    daemon_source = daemon_path.read_text(encoding="utf-8", errors="replace")
    if any(marker in daemon_source for marker in DAEMON_FORBIDDEN_TEXT):
        failures.append(f"daemon:{daemon_path.relative_to(ROOT)}")

    print(f"legacy entrypoint absence: failures={len(failures)}")
    for failure in failures:
        print(failure)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
