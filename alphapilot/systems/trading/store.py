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
import uuid

from alphapilot.systems.trading.domain import DeploymentLevel, LifecycleState, StrategyInstanceConfig


LATEST_SCHEMA_VERSION = 3


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
        with self._lock:
            previous = self._detect_schema_version()
            if previous and previous < LATEST_SCHEMA_VERSION:
                self._backup_before_migration(previous)
            db = sqlite3.connect(self.path, timeout=10.0)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("BEGIN IMMEDIATE")
                if previous == 0:
                    self._migrate_v1(db)
                    previous = 1
                if previous < 2:
                    self._migrate_v2(db)
                    previous = 2
                if previous < 3:
                    self._migrate_v3(db)
                    previous = 3
                if previous != LATEST_SCHEMA_VERSION:
                    raise RuntimeError(
                        f"unsupported strategy runtime schema {previous}; "
                        f"expected {LATEST_SCHEMA_VERSION}"
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def _detect_schema_version(self) -> int:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0
        with sqlite3.connect(self.path, timeout=10.0) as db:
            tables = {
                str(row[0])
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "schema_version" in tables:
                row = db.execute("SELECT version FROM schema_version WHERE singleton=1").fetchone()
                return int(row[0]) if row is not None else 0
            # Databases created by the first strategy-runtime release had no
            # version table.  Treat that exact shape as v1.
            return 1 if "strategy_instances" in tables else 0

    def _backup_before_migration(self, version: int) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.path.with_name(f"{self.path.name}.backup-v{version}-{stamp}")
        source = sqlite3.connect(self.path, timeout=10.0)
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return backup

    @staticmethod
    def _migrate_v1(db: sqlite3.Connection) -> None:
        _execute_script(
            db,
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
                CREATE TABLE IF NOT EXISTS schema_version (
                    singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
        )
        db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (1, 1, ?)",
            (_now(),),
        )

    @staticmethod
    def _migrate_v2(db: sqlite3.Connection) -> None:
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton=1), "
            "version INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(stage_evidence)")
        }
        if "config_hash" not in columns:
            db.execute(
                "ALTER TABLE stage_evidence ADD COLUMN config_hash TEXT NOT NULL DEFAULT ''"
            )
        db.execute(
            "UPDATE stage_evidence SET config_hash=(SELECT config_hash FROM strategy_instances "
            "WHERE strategy_instances.instance_id=stage_evidence.instance_id) "
            "WHERE config_hash=''"
        )
        _execute_script(
            db,
            """
            CREATE TABLE IF NOT EXISTS deployment_runtime (
                instance_id TEXT PRIMARY KEY,
                config_hash TEXT NOT NULL,
                deployment_level TEXT NOT NULL,
                account_id TEXT NOT NULL DEFAULT '',
                broker TEXT NOT NULL DEFAULT '',
                desired_state TEXT NOT NULL,
                observed_state TEXT NOT NULL,
                runtime_id TEXT NOT NULL DEFAULT '',
                runner_heartbeat_at TEXT NOT NULL DEFAULT '',
                last_command_id TEXT NOT NULL DEFAULT '',
                last_error_json TEXT NOT NULL DEFAULT '{}',
                reconcile_required INTEGER NOT NULL DEFAULT 0,
                binding_active INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE TABLE IF NOT EXISTS stage_runs (
                run_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL DEFAULT '',
                trading_sessions INTEGER NOT NULL DEFAULT 0,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE INDEX IF NOT EXISTS ix_stage_runs_instance_stage
                ON stage_runs(instance_id, stage, config_hash);
            CREATE TABLE IF NOT EXISTS route_blocks (
                scope_type TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                active INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope_type, scope_id)
            );
            """
        )
        db.execute(
            "INSERT OR IGNORE INTO deployment_runtime "
            "(instance_id, config_hash, deployment_level, account_id, broker, "
            "desired_state, observed_state, reconcile_required, binding_active, updated_at) "
            "SELECT si.instance_id, si.config_hash, si.deployment_level, "
            "COALESCE((SELECT de.account_id FROM deployment_events de "
            "WHERE de.instance_id=si.instance_id AND de.to_level='live' "
            "ORDER BY de.event_id DESC LIMIT 1), ''), "
            "COALESCE((SELECT de.broker FROM deployment_events de "
            "WHERE de.instance_id=si.instance_id AND de.to_level='live' "
            "ORDER BY de.event_id DESC LIMIT 1), ''), "
            "si.lifecycle, si.lifecycle, CASE WHEN si.deployment_level='live' THEN 1 ELSE 0 END, "
            "CASE WHEN si.deployment_level='live' THEN 1 ELSE 0 END, si.updated_at "
            "FROM strategy_instances si"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_live_account_writer "
            "ON deployment_runtime(account_id) "
            "WHERE deployment_level='live' AND binding_active=1 AND account_id<>''"
        )
        db.execute(
            "INSERT OR IGNORE INTO route_blocks VALUES ('global', '*', 0, '', ?)",
            (_now(),),
        )
        db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (1, 2, ?)",
            (_now(),),
        )

    @staticmethod
    def _migrate_v3(db: sqlite3.Connection) -> None:
        _execute_script(
            db,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_stage_run
                ON stage_runs(instance_id, stage, config_hash)
                WHERE status='running';
            CREATE TABLE IF NOT EXISTS stage_run_sessions (
                run_id TEXT NOT NULL,
                session TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, session),
                FOREIGN KEY (run_id) REFERENCES stage_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS stage_run_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES stage_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS ix_stage_run_events_run_type
                ON stage_run_events(run_id, event_type);
            """,
        )
        db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (1, 3, ?)",
            (_now(),),
        )

    @property
    def schema_version(self) -> int:
        return self._detect_schema_version()

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
            db.execute(
                "INSERT INTO deployment_runtime "
                "(instance_id, config_hash, deployment_level, desired_state, observed_state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    config.instance_id, config.config_hash, config.deployment_level,
                    LifecycleState.CREATED.value, LifecycleState.CREATED.value, now,
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
        allowed = {
            "params", "universe", "frequency", "data_policy", "portfolio_policy",
            "strategy_version", "strategy_code_hash", "model_hash",
        }
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
            persisted = db.execute(
                "SELECT config_hash, deployment_level FROM strategy_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if persisted is None:
                raise KeyError(f"unknown strategy instance {instance_id!r}")
            if (
                persisted["config_hash"] != current["config_hash"]
                or persisted["deployment_level"] != current["deployment_level"]
            ):
                raise RuntimeError("strategy instance changed concurrently during update")
            db.execute(
                "UPDATE strategy_instances SET strategy_version=?, config_json=?, config_hash=?, "
                "lifecycle=?, deployment_level=?, updated_at=? WHERE instance_id=?",
                (
                    config.strategy_version, _json(config.to_dict()), config.config_hash,
                    lifecycle, deployment, _now(), instance_id,
                ),
            )
            if changed:
                db.execute("DELETE FROM stage_evidence WHERE instance_id=?", (instance_id,))
                db.execute(
                    "UPDATE stage_runs SET status='invalidated', ended_at=?, updated_at=? "
                    "WHERE instance_id=? AND status='running'",
                    (_now(), _now(), instance_id),
                )
                requires_reconcile = current["deployment_level"] == DeploymentLevel.LIVE.value
                desired = (
                    LifecycleState.PAUSED.value
                    if requires_reconcile else LifecycleState.VALIDATED.value
                )
                observed = (
                    LifecycleState.PAUSED_PENDING_RECONCILE.value
                    if requires_reconcile else LifecycleState.VALIDATED.value
                )
                db.execute(
                    "UPDATE deployment_runtime SET config_hash=?, deployment_level=?, "
                    "desired_state=?, observed_state=?, runtime_id='', runner_heartbeat_at='', "
                    "last_command_id='', last_error_json='{}', reconcile_required=?, "
                    "binding_active=0, version=version+1, updated_at=? WHERE instance_id=?",
                    (
                        config.config_hash, deployment, desired, observed,
                        int(requires_reconcile), _now(), instance_id,
                    ),
                )
        return self.get_instance(instance_id)

    def set_lifecycle(self, instance_id: str, lifecycle: str) -> dict[str, Any]:
        value = LifecycleState(lifecycle).value
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE strategy_instances SET lifecycle=?, updated_at=? WHERE instance_id=?",
                (value, _now(), instance_id),
            )
        return self.get_instance(instance_id)

    def record_stage(
        self,
        instance_id: str,
        stage: str,
        *,
        passed: bool,
        details: Any = None,
        expected_config_hash: str | None = None,
    ) -> None:
        DeploymentLevel(stage)
        with self._lock, self._connect() as db:
            current = db.execute(
                "SELECT config_hash, deployment_level FROM strategy_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown strategy instance {instance_id!r}")
            if current["deployment_level"] != stage:
                raise ValueError(
                    f"cannot record {stage} evidence while instance is "
                    f"{current['deployment_level']}"
                )
            if expected_config_hash and current["config_hash"] != expected_config_hash:
                raise RuntimeError("strategy config changed while stage evidence was being produced")
            db.execute(
                "INSERT INTO stage_evidence "
                "(instance_id, stage, passed, details_json, updated_at, config_hash) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance_id, stage) DO UPDATE SET passed=excluded.passed, "
                "details_json=excluded.details_json, updated_at=excluded.updated_at, "
                "config_hash=excluded.config_hash",
                (
                    instance_id, stage, int(bool(passed)), _json(details or {}), _now(),
                    str(current["config_hash"]),
                ),
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
            persisted = db.execute(
                "SELECT * FROM strategy_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if persisted is None:
                raise KeyError(f"unknown strategy instance {instance_id!r}")
            persisted_current = _instance_row(persisted)
            if (
                persisted_current["config_hash"] != current["config_hash"]
                or persisted_current["deployment_level"] != source
            ):
                raise RuntimeError("strategy instance changed concurrently during promotion")
            current = persisted_current
            evidence = db.execute(
                "SELECT passed FROM stage_evidence WHERE instance_id=? AND stage=? AND config_hash=?",
                (instance_id, source, current["config_hash"]),
            ).fetchone()
            if evidence is None or not bool(evidence["passed"]):
                raise ValueError(f"passing {source} evidence is required before promotion")
            if target == DeploymentLevel.LIVE.value:
                if not account_id or not broker or not approval:
                    raise ValueError("live promotion requires account_id, broker and approval")
                other = db.execute(
                    "SELECT instance_id FROM deployment_runtime "
                    "WHERE deployment_level='live' AND binding_active=1 "
                    "AND instance_id<>? AND account_id=? LIMIT 1",
                    (instance_id, account_id),
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
            try:
                db.execute(
                    "UPDATE deployment_runtime SET config_hash=?, deployment_level=?, account_id=?, "
                    "broker=?, desired_state=?, observed_state=?, reconcile_required=?, "
                    "binding_active=?, version=version+1, updated_at=? WHERE instance_id=?",
                    (
                        current["config_hash"], target,
                        account_id if target == DeploymentLevel.LIVE.value else "",
                        broker if target == DeploymentLevel.LIVE.value else "",
                        LifecycleState.READY.value, LifecycleState.READY.value,
                        int(target == DeploymentLevel.LIVE.value),
                        int(target == DeploymentLevel.LIVE.value), _now(), instance_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"live account already has an automated writer for {account_id}") from exc
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
            "runtime": self.get_runtime_state(instance_id),
            "stage_runs": self.list_stage_runs(instance_id),
            "route_blocks": self.list_route_blocks(instance_id=instance_id),
        }

    def get_runtime_state(self, instance_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM deployment_runtime WHERE instance_id=?", (instance_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"deployment runtime is missing for {instance_id!r}")
        return _runtime_row(row)

    def update_runtime_state(
        self,
        instance_id: str,
        *,
        expected_version: int | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        allowed = {
            "config_hash", "deployment_level", "account_id", "broker",
            "desired_state", "observed_state", "runtime_id", "runner_heartbeat_at",
            "last_command_id", "last_error", "reconcile_required", "binding_active",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported runtime fields: {sorted(unknown)}")
        if not changes:
            return self.get_runtime_state(instance_id)
        values: dict[str, Any] = {}
        for key, value in changes.items():
            column = "last_error_json" if key == "last_error" else key
            if key == "last_error":
                value = _json(value or {})
            if key in {"reconcile_required", "binding_active"}:
                value = int(bool(value))
            values[column] = value
        assignments = ", ".join(f"{key}=?" for key in values)
        where = "instance_id=?"
        params = [*values.values(), _now(), instance_id]
        if expected_version is not None:
            where += " AND version=?"
            params.append(int(expected_version))
        try:
            with self._lock, self._connect() as db:
                cursor = db.execute(
                    f"UPDATE deployment_runtime SET {assignments}, version=version+1, "
                    f"updated_at=? WHERE {where}",
                    params,
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("deployment runtime changed concurrently")
        except sqlite3.IntegrityError as exc:
            raise ValueError("deployment binding conflicts with another active LIVE writer") from exc
        return self.get_runtime_state(instance_id)

    def transition_runtime(
        self,
        instance_id: str,
        *,
        lifecycle: str,
        expected_version: int | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        """Atomically update instance lifecycle and observed runtime projection."""

        lifecycle_value = LifecycleState(lifecycle).value
        allowed = {
            "config_hash", "deployment_level", "account_id", "broker",
            "desired_state", "observed_state", "runtime_id", "runner_heartbeat_at",
            "last_command_id", "last_error", "reconcile_required", "binding_active",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported runtime fields: {sorted(unknown)}")
        values: dict[str, Any] = {}
        for key, value in changes.items():
            column = "last_error_json" if key == "last_error" else key
            if key == "last_error":
                value = _json(value or {})
            if key in {"reconcile_required", "binding_active"}:
                value = int(bool(value))
            values[column] = value
        assignments = ", ".join(f"{key}=?" for key in values)
        runtime_sql = (
            f"UPDATE deployment_runtime SET {assignments + ', ' if assignments else ''}"
            "version=version+1, updated_at=? WHERE instance_id=?"
        )
        params = [*values.values(), _now(), instance_id]
        if expected_version is not None:
            runtime_sql += " AND version=?"
            params.append(int(expected_version))
        now = _now()
        try:
            with self._lock, self._connect() as db:
                cursor = db.execute(runtime_sql, params)
                if cursor.rowcount != 1:
                    raise RuntimeError("deployment runtime changed concurrently")
                cursor = db.execute(
                    "UPDATE strategy_instances SET lifecycle=?, updated_at=? WHERE instance_id=?",
                    (lifecycle_value, now, instance_id),
                )
                if cursor.rowcount != 1:
                    raise KeyError(f"unknown strategy instance {instance_id!r}")
        except sqlite3.IntegrityError as exc:
            raise ValueError("deployment binding conflicts with another active LIVE writer") from exc
        return self.get_runtime_state(instance_id)

    def record_runtime_heartbeat(
        self,
        instance_id: str,
        *,
        config_hash: str,
        runtime_id: str,
        heartbeat_at: str,
        observed_state: str,
    ) -> bool:
        """Refresh daemon truth only when its immutable binding still matches."""

        observed = LifecycleState(observed_state).value
        with self._lock, self._connect() as db:
            binding = db.execute(
                "SELECT desired_state, reconcile_required FROM deployment_runtime "
                "WHERE instance_id=? AND config_hash=? AND runtime_id=?",
                (instance_id, config_hash, runtime_id),
            ).fetchone()
            if binding is None:
                return False
            cursor = db.execute(
                "UPDATE deployment_runtime SET runner_heartbeat_at=?, observed_state=?, "
                "version=version+1, updated_at=? WHERE instance_id=? AND config_hash=? "
                "AND runtime_id=?",
                (heartbeat_at, observed, _now(), instance_id, config_hash, runtime_id),
            )
            if cursor.rowcount != 1:
                return False
            # Heartbeats report observed runner truth, but they must not erase a
            # coordinator HALTED/ERROR decision after a failed pause/stop. They
            # advance the public lifecycle only for an explicitly resumed,
            # reconciled deployment.
            if (
                binding["desired_state"] == LifecycleState.RUNNING.value
                and not bool(binding["reconcile_required"])
            ):
                db.execute(
                    "UPDATE strategy_instances SET lifecycle=?, updated_at=? WHERE instance_id=?",
                    (observed, _now(), instance_id),
                )
        return True

    def set_route_block(
        self,
        scope_type: str,
        scope_id: str,
        *,
        active: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        scope = str(scope_type).strip().lower()
        identifier = str(scope_id).strip()
        if scope not in {"global", "account", "instance"}:
            raise ValueError("scope_type must be global, account or instance")
        if scope == "global":
            identifier = "*"
        if not identifier:
            raise ValueError("scope_id is required")
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO route_blocks VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
                "active=excluded.active, reason=excluded.reason, updated_at=excluded.updated_at",
                (scope, identifier, int(bool(active)), str(reason), _now()),
            )
        return {
            "scope_type": scope,
            "scope_id": identifier,
            "active": bool(active),
            "reason": str(reason),
        }

    def active_route_blocks(self, *, instance_id: str, account_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM route_blocks WHERE active=1 AND "
                "((scope_type='global' AND scope_id='*') OR "
                "(scope_type='instance' AND scope_id=?) OR "
                "(scope_type='account' AND scope_id=?)) ORDER BY scope_type, scope_id",
                (instance_id, account_id),
            ).fetchall()
        return [_route_block_row(row) for row in rows]

    def list_route_blocks(self, *, instance_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if instance_id is None:
                rows = db.execute(
                    "SELECT * FROM route_blocks ORDER BY scope_type, scope_id"
                ).fetchall()
            else:
                runtime = db.execute(
                    "SELECT account_id FROM deployment_runtime WHERE instance_id=?",
                    (instance_id,),
                ).fetchone()
                account_id = "" if runtime is None else str(runtime["account_id"])
                rows = db.execute(
                    "SELECT * FROM route_blocks WHERE scope_type='global' OR "
                    "(scope_type='instance' AND scope_id=?) OR "
                    "(scope_type='account' AND scope_id=?) ORDER BY scope_type, scope_id",
                    (instance_id, account_id),
                ).fetchall()
        return [_route_block_row(row) for row in rows]

    def start_stage_run(self, instance_id: str, stage: str, *, run_id: str | None = None) -> dict[str, Any]:
        DeploymentLevel(stage)
        current = self.get_instance(instance_id)
        if current["deployment_level"] != stage:
            raise ValueError(
                f"cannot start {stage} run while instance is {current['deployment_level']}"
            )
        identifier = str(run_id or uuid.uuid4().hex)
        now = _now()
        with self._lock, self._connect() as db:
            persisted = db.execute(
                "SELECT config_hash, deployment_level FROM strategy_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if persisted is None:
                raise KeyError(f"unknown strategy instance {instance_id!r}")
            if (
                persisted["config_hash"] != current["config_hash"]
                or persisted["deployment_level"] != stage
            ):
                raise RuntimeError("strategy instance changed concurrently before stage run start")
            db.execute(
                "INSERT INTO stage_runs "
                "(run_id, instance_id, stage, config_hash, status, started_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?)",
                (identifier, instance_id, stage, current["config_hash"], now, now),
            )
        return self.get_stage_run(identifier)

    def get_active_stage_run(
        self,
        instance_id: str,
        *,
        stage: str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_instance(instance_id)
        with self._connect() as db:
            if stage is None:
                row = db.execute(
                    "SELECT * FROM stage_runs WHERE instance_id=? AND config_hash=? "
                    "AND status='running' ORDER BY started_at DESC LIMIT 1",
                    (instance_id, current["config_hash"]),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM stage_runs WHERE instance_id=? AND stage=? AND config_hash=? "
                    "AND status='running' ORDER BY started_at DESC LIMIT 1",
                    (instance_id, stage, current["config_hash"]),
                ).fetchone()
        return None if row is None else _stage_run_row(row)

    def record_stage_session(
        self,
        instance_id: str,
        *,
        config_hash: str,
        stage: str,
        session: str,
    ) -> bool:
        with self._lock, self._connect() as db:
            run = db.execute(
                "SELECT run_id FROM stage_runs WHERE instance_id=? AND stage=? "
                "AND config_hash=? AND status='running' ORDER BY started_at DESC LIMIT 1",
                (instance_id, stage, config_hash),
            ).fetchone()
            if run is None:
                return False
            cursor = db.execute(
                "INSERT OR IGNORE INTO stage_run_sessions VALUES (?, ?, ?)",
                (run["run_id"], str(session)[:10], _now()),
            )
            db.execute(
                "UPDATE stage_runs SET trading_sessions=(SELECT COUNT(*) FROM stage_run_sessions "
                "WHERE run_id=?), updated_at=? WHERE run_id=?",
                (run["run_id"], _now(), run["run_id"]),
            )
            return cursor.rowcount == 1

    def record_stage_event(
        self,
        instance_id: str,
        *,
        config_hash: str,
        stage: str,
        event_type: str,
        count: int = 1,
        details: Any = None,
    ) -> bool:
        with self._lock, self._connect() as db:
            run = db.execute(
                "SELECT run_id FROM stage_runs WHERE instance_id=? AND stage=? "
                "AND config_hash=? AND status='running' ORDER BY started_at DESC LIMIT 1",
                (instance_id, stage, config_hash),
            ).fetchone()
            if run is None:
                return False
            db.execute(
                "INSERT INTO stage_run_events "
                "(run_id, event_type, count, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run["run_id"], str(event_type), max(int(count), 0), _json(details or {}), _now()),
            )
            return True

    def finish_stage_run(
        self,
        run_id: str,
        *,
        trading_sessions: int,
        metrics: dict[str, Any] | None = None,
        status: str = "completed",
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("stage run status must be completed, failed or cancelled")
        with self._lock, self._connect() as db:
            run = db.execute(
                "SELECT * FROM stage_runs WHERE run_id=? AND status='running'", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"stage run {run_id!r} is missing or already terminal")
            event_rows = db.execute(
                "SELECT event_type, SUM(count) AS total FROM stage_run_events "
                "WHERE run_id=? GROUP BY event_type",
                (run_id,),
            ).fetchall()
            derived = {str(row["event_type"]): int(row["total"] or 0) for row in event_rows}
            derived["decisions"] = int(db.execute(
                "SELECT COUNT(*) FROM decisions WHERE instance_id=? AND config_hash=? AND created_at>=?",
                (run["instance_id"], run["config_hash"], run["started_at"]),
            ).fetchone()[0])
            derived["plans"] = int(db.execute(
                "SELECT COUNT(*) FROM execution_plans WHERE instance_id=? AND created_at>=?",
                (run["instance_id"], run["started_at"]),
            ).fetchone()[0])
            derived["rejected_orders"] = int(db.execute(
                "SELECT COUNT(*) FROM child_orders co JOIN execution_plans ep ON ep.plan_id=co.plan_id "
                "WHERE ep.instance_id=? AND ep.created_at>=? AND co.status='rejected'",
                (run["instance_id"], run["started_at"]),
            ).fetchone()[0])
            combined = dict(derived)
            for key, value in (metrics or {}).items():
                if key in combined and isinstance(value, (int, float, bool)):
                    combined[key] = _metric_count(combined[key]) + _metric_count(value)
                else:
                    combined[key] = value
            recorded_sessions = int(db.execute(
                "SELECT COUNT(*) FROM stage_run_sessions WHERE run_id=?", (run_id,)
            ).fetchone()[0])
            declared_sessions = max(int(trading_sessions), 0)
            if declared_sessions != recorded_sessions:
                combined["declared_trading_sessions"] = declared_sessions
            cursor = db.execute(
                "UPDATE stage_runs SET status=?, ended_at=?, trading_sessions=?, metrics_json=?, "
                "updated_at=? WHERE run_id=? AND status='running'",
                (
                    status, _now(), recorded_sessions, _json(combined),
                    _now(), run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"stage run {run_id!r} is missing or already terminal")
        return self.get_stage_run(run_id)

    def get_stage_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM stage_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown stage run {run_id!r}")
        return _stage_run_row(row)

    def list_stage_runs(self, instance_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM stage_runs WHERE instance_id=? ORDER BY started_at, run_id",
                (instance_id,),
            ).fetchall()
        return [_stage_run_row(row) for row in rows]

    def evaluate_stage(
        self,
        instance_id: str,
        stage: str,
        *,
        minimum_sessions: int,
    ) -> dict[str, Any]:
        DeploymentLevel(stage)
        current = self.get_instance(instance_id)
        runs = [
            row for row in self.list_stage_runs(instance_id)
            if row["stage"] == stage
            and row["config_hash"] == current["config_hash"]
            and row["status"] == "completed"
        ]
        run_ids = [row["run_id"] for row in runs]
        sessions = 0
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            with self._connect() as db:
                sessions = int(db.execute(
                    f"SELECT COUNT(DISTINCT session) FROM stage_run_sessions "
                    f"WHERE run_id IN ({placeholders})",
                    run_ids,
                ).fetchone()[0])
        metric_names = (
            "unresolved_errors", "duplicate_routes", "position_breaches",
            "reconciliation_warnings",
        )
        failures = {
            key: sum(_metric_count(row["metrics"].get(key)) for row in runs)
            for key in metric_names
        }
        passed = sessions >= int(minimum_sessions) and not any(failures.values())
        details = {
            "config_hash": current["config_hash"],
            "minimum_sessions": int(minimum_sessions),
            "trading_sessions": sessions,
            "runs": len(runs),
            "failures": failures,
        }
        self.record_stage(
            instance_id,
            stage,
            passed=passed,
            details=details,
            expected_config_hash=current["config_hash"],
        )
        return {"passed": passed, **details}

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


def _runtime_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "instance_id": row["instance_id"],
        "config_hash": row["config_hash"],
        "deployment_level": row["deployment_level"],
        "account_id": row["account_id"],
        "broker": row["broker"],
        "desired_state": row["desired_state"],
        "observed_state": row["observed_state"],
        "runtime_id": row["runtime_id"],
        "runner_heartbeat_at": row["runner_heartbeat_at"],
        "last_command_id": row["last_command_id"],
        "last_error": json.loads(row["last_error_json"] or "{}"),
        "reconcile_required": bool(row["reconcile_required"]),
        "binding_active": bool(row["binding_active"]),
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


def _route_block_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "active": bool(row["active"]),
        "reason": row["reason"],
        "updated_at": row["updated_at"],
    }


def _stage_run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "instance_id": row["instance_id"],
        "stage": row["stage"],
        "config_hash": row["config_hash"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "trading_sessions": int(row["trading_sessions"]),
        "metrics": json.loads(row["metrics_json"] or "{}"),
        "updated_at": row["updated_at"],
    }


def _metric_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return max(int(value), 0)
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return int(bool(value))


def _execute_script(db: sqlite3.Connection, script: str) -> None:
    """Execute simple migration DDL without ``executescript`` auto-commits.

    ``sqlite3.Connection.executescript`` commits an open transaction before
    running its input.  Migrations must remain inside the explicit
    ``BEGIN IMMEDIATE`` transaction established by :meth:`_init_schema`.
    Migration statements intentionally contain no triggers or semicolons in
    string literals, so a small statement splitter is sufficient here.
    """

    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            db.execute(sql)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
