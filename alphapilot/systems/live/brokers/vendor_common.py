"""Deprecated XTP-family helper import path.

Install ``alphapilot-broker-xcommon``. New broker plugins should import from
``alphapilot_broker_xcommon.vendor_common`` directly.
"""

try:
    from alphapilot_broker_xcommon.vendor_common import *  # noqa: F403
except ImportError as exc:  # pragma: no cover - depends on optional plugin
    raise ImportError(
        "XTP-family helpers moved to the 'alphapilot-broker-xcommon' package"
    ) from exc
