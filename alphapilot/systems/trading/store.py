"""Crash-safe SQLite journal for strategy instances and deployments."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator

from alphapilot.systems.trading.domain import DeploymentLevel, LifecycleState, StrategyInstanceConfig


class StrategyRuntimeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_instances (
                    instance_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    deployment_level TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS stage_evidence (
                    instance_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    details_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (instance_id, stage),
                    FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
                );
                CREATE TABLE IF NOT EXISTS deployment_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id TEXT NOT NULL,
                    from_level TEXT NOT NULL,
                    to_level TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    account_id TEXT NOT NULL DEFAULT '',
                    broker TEXT NOT NULL DEFAULT '',
                    approval TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS execution_plans (
                    plan_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS child_orders (
                    reference TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL,
                    order_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def create_instance(self, config: StrategyInstanceConfig) -> dict[str, Any]:
        if not config.instance_id:
            raise ValueError("instance_id is required")
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO strategy_instances VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    config.instance_id, config.strategy_id, config.strategy_version,
                    _json(config.to_dict()), config.config_hash,
                    LifecycleState.CREATED.value, config.deployment_level, now,
                ),
            )
        return self.get_instance(config.instance_id)

    def get_instance(self, instance_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM strategy_instances WHERE instance_id=?", (instance_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown strategy instance {instance_id!r}")
        return _instance_row(row)

    def list_instances(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM strategy_instances ORDER BY instance_id").fetchall()
        return [_instance_row(row) for row in rows]

    def update_instance(self, instance_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.get_instance(instance_id)
        payload = dict(current["config"])
        allowed = {"params", "universe", "frequency", "data_policy", "portfolio_policy"}
        payload.update({key: value for key, value in changes.items() if key in allowed})
        payload["config_hash"] = ""
        config = StrategyInstanceConfig.from_dict(payload)
        changed = config.config_hash != current["config_hash"]
        deployment = (
            DeploymentLevel.SHADOW.value
            if changed and current["deployment_level"] == DeploymentLevel.LIVE.value
            else DeploymentLevel.REPLAY.value
            if changed
            else current["deployment_level"]
        )
        lifecycle = LifecycleState.VALIDATED.value if changed else current["lifecycle"]
        config.deployment_level = deployment
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE strategy_instances SET config_json=?, config_hash=?, lifecycle=?, "
                "deployment_level=?, updated_at=? WHERE instance_id=?",
                (_json(config.to_dict()), config.config_hash, lifecycle, deployment, _now(), instance_id),
            )
            if changed:
                db.execute("DELETE FROM stage_evidence WHERE instance_id=?", (instance_id,))
        return self.get_instance(instance_id)

    def set_lifecycle(self, instance_id: str, lifecycle: str) -> dict[str, Any]:
        value = LifecycleState(lifecycle).value
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE strategy_instances SET lifecycle=?, updated_at=? WHERE instance_id=?",
                (value, _now(), instance_id),
            )
        return self.get_instance(instance_id)

    def record_stage(self, instance_id: str, stage: str, *, passed: bool, details: Any = None) -> None:
        DeploymentLevel(stage)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO stage_evidence VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(instance_id, stage) DO UPDATE SET passed=excluded.passed, "
                "details_json=excluded.details_json, updated_at=excluded.updated_at",
                (instance_id, stage, int(bool(passed)), _json(details or {}), _now()),
            )

    def promote(
        self,
        instance_id: str,
        to_level: str,
        *,
        account_id: str = "",
        broker: str = "",
        approval: str = "",
    ) -> dict[str, Any]:
        target = DeploymentLevel(to_level).value
        current = self.get_instance(instance_id)
        source = current["deployment_level"]
        ladder = [item.value for item in DeploymentLevel]
        if ladder.index(target) != ladder.index(source) + 1:
            raise ValueError(f"deployment promotion must be sequential: {source} -> {target}")
        with self._lock, self._connect() as db:
            evidence = db.execute(
                "SELECT passed FROM stage_evidence WHERE instance_id=? AND stage=?",
                (instance_id, source),
            ).fetchone()
            if evidence is None or not bool(evidence["passed"]):
                raise ValueError(f"passing {source} evidence is required before promotion")
            if target == DeploymentLevel.LIVE.value:
                if not account_id or not broker or not approval:
                    raise ValueError("live promotion requires account_id, broker and approval")
                other = db.execute(
                    "SELECT si.instance_id FROM strategy_instances si "
                    "JOIN deployment_events de ON de.instance_id=si.instance_id "
                    "WHERE si.deployment_level='live' AND si.instance_id<>? "
                    "AND de.to_level='live' AND de.account_id=? "
                    "ORDER BY de.event_id DESC LIMIT 1", (instance_id, account_id),
                ).fetchone()
                if other is not None:
                    raise ValueError(f"live account already has writer {other['instance_id']}")
            config_payload = dict(current["config"])
            config_payload["deployment_level"] = target
            approval_digest = (
                hashlib.sha256(
                    f"{instance_id}:{current['config_hash']}:{account_id}:{broker}:{approval}".encode("utf-8")
                ).hexdigest()
                if approval else ""
            )
            db.execute(
                "UPDATE strategy_instances SET deployment_level=?, lifecycle=?, config_json=?, updated_at=? "
                "WHERE instance_id=?",
                (target, LifecycleState.READY.value, _json(config_payload), _now(), instance_id),
            )
            db.execute(
                "INSERT INTO deployment_events "
                "(instance_id, from_level, to_level, config_hash, account_id, broker, approval, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (instance_id, source, target, current["config_hash"], account_id, broker, approval_digest, _now()),
                # approval is stored as a binding digest, never as the operator secret.
            )
        return self.get_instance(instance_id)

    def deployment(self, instance_id: str) -> dict[str, Any]:
        current = self.get_instance(instance_id)
        with self._connect() as db:
            events = db.execute(
                "SELECT * FROM deployment_events WHERE instance_id=? ORDER BY event_id", (instance_id,)
            ).fetchall()
            evidence = db.execute(
                "SELECT * FROM stage_evidence WHERE instance_id=? ORDER BY stage", (instance_id,)
            ).fetchall()
        return {
            "instance": current,
            "events": [dict(row) for row in events],
            "evidence": [
                {**dict(row), "passed": bool(row["passed"]), "details": json.loads(row["details_json"])}
                for row in evidence
            ],
        }

    def record_decision(self, decision_id: str, instance_id: str, config_hash: str, payload: Any) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, ?)",
                (decision_id, instance_id, config_hash, _json(payload), _now()),
            )
            return cursor.rowcount == 1

    def record_plan(self, plan_id: str, decision_id: str, instance_id: str, payload: Any, status: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO execution_plans VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, decision_id, instance_id, _json(payload), status, _now()),
            )
            return cursor.rowcount == 1

    def record_child_order(self, reference: str, plan_id: str, payload: Any, *, status: str, order_id: str = "") -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO child_orders VALUES (?, ?, ?, ?, ?, ?)",
                (reference, plan_id, order_id, status, _json(payload), _now()),
            )
            return cursor.rowcount == 1

    def update_child_order(self, reference: str, *, status: str, order_id: str = "") -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE child_orders SET status=?, order_id=?, updated_at=? WHERE reference=?",
                (status, order_id, _now(), reference),
            )


def _instance_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "instance_id": row["instance_id"],
        "strategy_id": row["strategy_id"],
        "strategy_version": row["strategy_version"],
        "config": json.loads(row["config_json"]),
        "config_hash": row["config_hash"],
        "lifecycle": row["lifecycle"],
        "deployment_level": row["deployment_level"],
        "updated_at": row["updated_at"],
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
