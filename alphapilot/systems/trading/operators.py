"""Local operator authentication, one-shot LIVE approvals and audit helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Any
import uuid

from alphapilot.systems.trading.contracts import OperatorContext


class OperatorAuthService:
    """Authenticate high-entropy local tokens without persisting plaintext."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def generate_token(
        self,
        operator_id: str,
        *,
        label: str = "",
        expires_in_days: int | None = None,
    ) -> dict[str, Any]:
        operator = str(operator_id).strip()
        if not operator:
            raise ValueError("operator_id is required")
        token_id = uuid.uuid4().hex[:16]
        plaintext = f"apop_{token_id}_{secrets.token_urlsafe(48)}"
        expires_at = ""
        if expires_in_days is not None:
            if int(expires_in_days) <= 0:
                raise ValueError("expires_in_days must be positive")
            expires_at = _iso(_now() + timedelta(days=int(expires_in_days)))
        record = self.store.create_operator_token(
            token_id,
            operator,
            _digest(plaintext),
            label=str(label),
            expires_at=expires_at,
        )
        # The caller is the only place where plaintext is returned.
        return {**record, "token": plaintext}

    def authenticate(
        self,
        authorization: str,
        *,
        request_id: str,
        reason: str = "",
    ) -> OperatorContext:
        raw = str(authorization or "").strip()
        if not raw.lower().startswith("bearer "):
            raise PermissionError("a Bearer operator token is required")
        plaintext = raw.split(None, 1)[1].strip()
        if not plaintext:
            raise PermissionError("operator token is empty")
        record = self.store.authenticate_operator_token(_digest(plaintext))
        if record is None:
            raise PermissionError("operator token is invalid, expired or revoked")
        return OperatorContext(
            operator_id=str(record["operator_id"]),
            request_id=str(request_id or uuid.uuid4().hex),
            reason=str(reason),
            auth_source=f"local-token:{record['token_id']}",
        )

    def issue_live_approval(
        self,
        operator: OperatorContext,
        *,
        instance_id: str,
        config_hash: str,
        account_id: str,
        broker: str,
        reason: str,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        ttl = int(ttl_seconds)
        if ttl < 30 or ttl > 3600:
            raise ValueError("LIVE approval ttl_seconds must be between 30 and 3600")
        if not reason.strip():
            raise ValueError("LIVE approval requires an operator reason")
        approval_id = uuid.uuid4().hex
        plaintext = f"apla_{approval_id[:16]}_{secrets.token_urlsafe(32)}"
        record = self.store.create_live_approval(
            approval_id,
            _digest(plaintext),
            operator_id=operator.operator_id,
            instance_id=instance_id,
            config_hash=config_hash,
            account_id=account_id,
            broker=broker,
            reason=reason,
            expires_at=_iso(_now() + timedelta(seconds=ttl)),
        )
        self.audit(
            operator,
            action="authorize_live",
            result="issued",
            instance_id=instance_id,
            config_hash=config_hash,
            account_id=account_id,
            broker=broker,
            details={"approval_id": approval_id, "expires_at": record["expires_at"]},
        )
        return {**record, "approval": plaintext}

    def consume_live_approval(
        self,
        plaintext: str,
        *,
        instance_id: str,
        config_hash: str,
        account_id: str,
        broker: str,
    ) -> dict[str, Any]:
        if not str(plaintext).startswith("apla_"):
            raise ValueError("a one-time LIVE approval is required")
        return self.store.consume_live_approval(
            _digest(str(plaintext)),
            instance_id=instance_id,
            config_hash=config_hash,
            account_id=account_id,
            broker=broker,
        )

    def audit(
        self,
        operator: OperatorContext,
        *,
        action: str,
        result: str,
        instance_id: str = "",
        config_hash: str = "",
        account_id: str = "",
        broker: str = "",
        details: Any = None,
    ) -> int:
        return self.store.record_audit_event(
            operator_id=operator.operator_id,
            request_id=operator.request_id,
            action=action,
            reason=operator.reason,
            auth_source=operator.auth_source,
            result=result,
            instance_id=instance_id,
            config_hash=config_hash,
            account_id=account_id,
            broker=broker,
            details=details,
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")
