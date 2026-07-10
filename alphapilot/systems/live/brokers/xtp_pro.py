"""Deprecated XTP Pro import path; install ``alphapilot-broker-xtp``."""

try:
    from alphapilot_broker_xtp.gateway import *  # noqa: F403
    from alphapilot_broker_xtp.gateway import SDK_AVAILABLE, XtpProGateway
except ImportError as exc:  # pragma: no cover - depends on optional plugin
    raise ImportError(
        "XTP support moved to the 'alphapilot-broker-xtp' package; install it and restart AlphaPilot"
    ) from exc

__all__ = ["SDK_AVAILABLE", "XtpProGateway"]
