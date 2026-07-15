"""Credential redaction shared by broker logs and UAT evidence."""

from __future__ import annotations

import os
import re
from typing import Any


_SENSITIVE_MARKERS = (
    "password", "passwd", "secret", "token", "software_key", "api_key",
    "private_key", "credential",
)
_ENV_SENSITIVE_MARKERS = (*_SENSITIVE_MARKERS, "account")


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "********"
                if any(marker in str(key).lower() for marker in _SENSITIVE_MARKERS)
                else redact_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        text = value
        for key, secret in os.environ.items():
            if (
                secret
                and len(secret) >= 3
                and any(marker.upper() in key.upper() for marker in _ENV_SENSITIVE_MARKERS)
            ):
                text = text.replace(secret, "********")
        text = re.sub(
            r"(?i)(password|passwd|secret|token|software_key|api_key|private_key)"
            r"\s*[:=]\s*([^\s,;]+)",
            r"\1=********",
            text,
        )
        return text
    return value
