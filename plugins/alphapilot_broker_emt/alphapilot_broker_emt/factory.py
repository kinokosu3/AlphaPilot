"""Lazy EMT gateway construction and SDK availability probe."""

from __future__ import annotations


def create_gateway(*, name: str, roles: frozenset[str]):
    from alphapilot_broker_emt.gateway import EmtGateway

    return EmtGateway(name=name, roles=roles)


def check_available() -> dict[str, object]:
    from alphapilot_broker_emt.gateway import SDK_AVAILABLE

    return {
        "ok": SDK_AVAILABLE,
        "detail": "alphapilot_emt.api available" if SDK_AVAILABLE else "compiled alphapilot_emt.api bindings unavailable",
    }
