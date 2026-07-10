"""Deprecated EMT import path; install ``alphapilot-broker-emt``."""

try:
    from alphapilot_broker_emt.gateway import *  # noqa: F403
    from alphapilot_broker_emt.gateway import EmtGateway, SDK_AVAILABLE
except ImportError as exc:  # pragma: no cover - depends on optional plugin
    raise ImportError(
        "EMT support moved to the 'alphapilot-broker-emt' package; install it and restart AlphaPilot"
    ) from exc

__all__ = ["EmtGateway", "SDK_AVAILABLE"]
