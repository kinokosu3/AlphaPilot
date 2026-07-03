"""Shared helpers for XTP live smoke/preflight scripts."""

from __future__ import annotations

from collections.abc import Mapping


PUBLIC_TEST_ENDPOINT_ENV: dict[str, str] = {
    # Public XTP simulation endpoints historically used by vn.py tutorials.
    # They may change; values supplied by the broker/account email should be
    # passed through env and will override these defaults.
    "ALPHAPILOT_LIVE_XTP_CLIENT_ID": "1",
    "ALPHAPILOT_LIVE_XTP_QUOTE_HOST": "120.27.164.138",
    "ALPHAPILOT_LIVE_XTP_QUOTE_PORT": "6002",
    "ALPHAPILOT_LIVE_XTP_TRADE_HOST": "120.27.164.69",
    "ALPHAPILOT_LIVE_XTP_TRADE_PORT": "6002",
    "ALPHAPILOT_LIVE_XTP_QUOTE_PROTOCOL": "TCP",
    "ALPHAPILOT_LIVE_XTP_LOG_LEVEL": "INFO",
}


def env_with_public_test_endpoints(base: Mapping[str, str]) -> dict[str, str]:
    """Return env with public test endpoints filled where the caller omitted them."""
    merged = dict(base)
    for key, value in PUBLIC_TEST_ENDPOINT_ENV.items():
        merged.setdefault(key, value)
    return merged
