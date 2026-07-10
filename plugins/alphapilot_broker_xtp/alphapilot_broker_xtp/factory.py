"""Lazy XTP gateway construction and SDK availability probe."""

from __future__ import annotations


def create_gateway(*, name: str, roles: frozenset[str]):
    from alphapilot_broker_xtp.gateway import XtpProGateway

    return XtpProGateway(name=name, roles=roles)


def check_available() -> dict[str, object]:
    from alphapilot_broker_xtp.gateway import SDK_AVAILABLE

    return {
        "ok": SDK_AVAILABLE,
        "detail": "alphapilot_xtpx.api available" if SDK_AVAILABLE else "compiled alphapilot_xtpx.api bindings unavailable",
    }
