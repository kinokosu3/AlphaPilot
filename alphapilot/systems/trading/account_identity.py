"""Non-secret account identity helpers for control-plane comparisons."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any


ACCOUNT_HASH_PREFIX = "sha256:"


def account_identity_hash(account_id: str) -> str:
    """Return a tagged hash, preserving an already-tagged identity."""

    value = str(account_id or "").strip()
    if not value:
        return ""
    if value.startswith(ACCOUNT_HASH_PREFIX):
        return value.lower()
    return ACCOUNT_HASH_PREFIX + hashlib.sha256(value.encode("utf-8")).hexdigest()


def account_identities_match(expected: str, observed: str) -> bool:
    """Compare raw or tagged account identities without exposing either one."""

    left = account_identity_hash(expected)
    right = account_identity_hash(observed)
    return bool(left and right and hmac.compare_digest(left, right))


def public_account_state(value: Any) -> Any:
    """Recursively replace ``account_id`` fields with a tagged hash."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if str(key) == "account_id":
                if item not in (None, ""):
                    result["account_id_hash"] = account_identity_hash(str(item))
                continue
            result[str(key)] = public_account_state(item)
        return result
    if isinstance(value, list):
        return [public_account_state(item) for item in value]
    if isinstance(value, tuple):
        return tuple(public_account_state(item) for item in value)
    return value
