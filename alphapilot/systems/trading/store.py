"""Crash-safe SQLite journal for strategy instances and deployments."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterator
import uuid

from alphapilot.systems.trading.domain import (
    DeploymentMode,
    DeploymentSpec,
    ExecutionEnvironment,
    InstanceValidationState,
    LifecycleState,
    StrategyInstanceConfig,
)
from alphapilot.systems.trading.account_identity import account_identity_hash


LATEST_SCHEMA_VERSION = 10

# The v1-v9 DDL builders are retained only to construct an empty database before
# the destructive v10 normalization. Existing v1-v9 files are never migrated.


class StrategyRuntimeStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._environment_id = ""
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
            if previous not in {0, LATEST_SCHEMA_VERSION}:
                raise RuntimeError(
                    f"strategy runtime schema v{previous} is incompatible with v10; "
                    "configure a new ALPHAPILOT_STRATEGY_RUNTIME_STORE path"
                )
            db = sqlite3.connect(self.path, timeout=10.0)
            db.row_factory = sqlite3.Row
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
                if previous < 4:
                    self._migrate_v4(db)
                    previous = 4
                if previous < 5:
                    self._migrate_v5(db)
                    previous = 5
                if previous < 6:
                    self._migrate_v6(db)
                    previous = 6
                if previous < 7:
                    self._migrate_v7(db)
                    previous = 7
                if previous < 8:
                    self._migrate_v8(db)
                    previous = 8
                if previous < 9:
                    self._migrate_v9(db)
                    previous = 9
                if previous < 10:
                    self._migrate_v10(db)
                    previous = 10
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
        # Version detection for an existing file is deliberately read-only:
        # legacy v1-v9 stores must be rejected before SQLite can start a write
        # transaction or recover them in place.
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=10.0) as db:
            tables = {
                str(row[0])
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "schema_version" in tables:
                row = db.execute("SELECT version FROM schema_version WHERE singleton=1").fetchone()
                if row is None or int(row[0]) <= 0:
                    raise RuntimeError(
                        "strategy runtime schema metadata is missing or invalid; "
                        "configure a new ALPHAPILOT_STRATEGY_RUNTIME_STORE path"
                    )
                return int(row[0])
            # Databases created by the first strategy-runtime release had no
            # version table.  Treat that exact shape as v1.
            if "strategy_instances" in tables:
                return 1
            if tables:
                raise RuntimeError(
                    "strategy runtime path contains a non-empty unknown SQLite schema; "
                    "configure a new ALPHAPILOT_STRATEGY_RUNTIME_STORE path"
                )
            return 0

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

    @staticmethod
    def _migrate_v4(db: sqlite3.Connection) -> None:
        _execute_script(
            db,
            """
            CREATE TABLE IF NOT EXISTS artifact_manifests (
                artifact_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(instance_id, config_hash),
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE TABLE IF NOT EXISTS signal_envelopes (
                signal_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                as_of TEXT NOT NULL,
                signal_kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE INDEX IF NOT EXISTS ix_signal_envelopes_instance_asof
                ON signal_envelopes(instance_id, config_hash, as_of);
            CREATE TABLE IF NOT EXISTS portfolio_decisions (
                decision_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                as_of TEXT NOT NULL,
                effective_session TEXT NOT NULL,
                valid_until TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE INDEX IF NOT EXISTS ix_portfolio_decisions_effective
                ON portfolio_decisions(instance_id, config_hash, effective_session, status);
            CREATE TABLE IF NOT EXISTS provider_checkpoints (
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                state_schema_version INTEGER NOT NULL,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (instance_id, config_hash),
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE TABLE IF NOT EXISTS backtest_runs (
                run_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                artifact_dir TEXT NOT NULL DEFAULT '',
                error_json TEXT NOT NULL DEFAULT '{}',
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL DEFAULT '',
                ended_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE INDEX IF NOT EXISTS ix_backtest_runs_instance
                ON backtest_runs(instance_id, config_hash, created_at);
            CREATE TABLE IF NOT EXISTS execution_plan_state (
                plan_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                phase TEXT NOT NULL,
                recovery_version INTEGER NOT NULL DEFAULT 1,
                next_child_index_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL,
                last_error_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE TABLE IF NOT EXISTS plan_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(plan_id, phase, attempt),
                FOREIGN KEY (plan_id) REFERENCES execution_plan_state(plan_id)
            );
            CREATE TABLE IF NOT EXISTS order_reconciliation (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id TEXT NOT NULL,
                reference TEXT NOT NULL,
                local_status TEXT NOT NULL,
                broker_status TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (plan_id) REFERENCES execution_plan_state(plan_id)
            );
            CREATE TABLE IF NOT EXISTS fill_reconciliation (
                fill_key TEXT PRIMARY KEY,
                plan_id TEXT NOT NULL,
                reference TEXT NOT NULL,
                order_id TEXT NOT NULL,
                volume REAL NOT NULL,
                price REAL NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (plan_id) REFERENCES execution_plan_state(plan_id)
            );
            """,
        )
        _rehash_instances_for_v4(db)
        db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (1, 4, ?)",
            (_now(),),
        )

    @staticmethod
    def _migrate_v5(db: sqlite3.Connection) -> None:
        _execute_script(
            db,
            """
            CREATE TABLE IF NOT EXISTS operator_tokens (
                token_id TEXT PRIMARY KEY,
                operator_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL DEFAULT '',
                revoked_at TEXT NOT NULL DEFAULT '',
                last_used_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS live_approvals (
                approval_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                operator_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                account_id TEXT NOT NULL,
                broker TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT NOT NULL DEFAULT '',
                revoked_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE TABLE IF NOT EXISTS operator_audit_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                auth_source TEXT NOT NULL,
                instance_id TEXT NOT NULL DEFAULT '',
                config_hash TEXT NOT NULL DEFAULT '',
                account_id TEXT NOT NULL DEFAULT '',
                broker TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_operator_audit_created
                ON operator_audit_events(created_at, event_id);
            CREATE TABLE IF NOT EXISTS account_baselines (
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                account_id TEXT NOT NULL,
                positions_json TEXT NOT NULL,
                confirmed_by TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                PRIMARY KEY (instance_id, config_hash),
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE TABLE IF NOT EXISTS legacy_usage (
                entrypoint TEXT PRIMARY KEY,
                call_count INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            """,
        )
        db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (1, 5, ?)",
            (_now(),),
        )

    @staticmethod
    def _migrate_v6(db: sqlite3.Connection) -> None:
        active = db.execute(
            "SELECT instance_id FROM deployment_runtime WHERE binding_active=1 "
            "OR desired_state IN ('running', 'warming_up') "
            "OR observed_state IN ('running', 'warming_up') LIMIT 1"
        ).fetchone()
        if active is not None:
            raise RuntimeError(
                "strategy runtime schema v6 changes immutable config hashes; "
                f"stop active instance {active['instance_id']!r} before migration"
            )
        checkpoint_columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(provider_checkpoints)")
        }
        if "last_evaluated_as_of" not in checkpoint_columns:
            db.execute(
                "ALTER TABLE provider_checkpoints ADD COLUMN "
                "last_evaluated_as_of TEXT NOT NULL DEFAULT ''"
            )
        if "state_hash" not in checkpoint_columns:
            db.execute(
                "ALTER TABLE provider_checkpoints ADD COLUMN state_hash TEXT NOT NULL DEFAULT ''"
            )
        backtest_columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(backtest_runs)")
        }
        if "origin" not in backtest_columns:
            db.execute(
                "ALTER TABLE backtest_runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'trading'"
            )
        if "legacy_job_id" not in backtest_columns:
            db.execute(
                "ALTER TABLE backtest_runs ADD COLUMN legacy_job_id TEXT NOT NULL DEFAULT ''"
            )
        _execute_script(
            db,
            """
            CREATE TABLE IF NOT EXISTS compatibility_entrypoints (
                entrypoint TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                replacement TEXT NOT NULL,
                deprecated_since TEXT NOT NULL,
                sunset_at TEXT NOT NULL,
                removal_release TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'deprecated',
                migration_cutoff TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS legacy_usage_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                entrypoint TEXT NOT NULL,
                client_kind TEXT NOT NULL DEFAULT '',
                client_version TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                request_id_hash TEXT NOT NULL DEFAULT '',
                environment_id TEXT NOT NULL DEFAULT '',
                used_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_legacy_usage_events_entrypoint
                ON legacy_usage_events(entrypoint, used_at);
            CREATE TABLE IF NOT EXISTS compatibility_environments (
                environment_id TEXT PRIMARY KEY,
                registered_at TEXT NOT NULL,
                last_reported_at TEXT NOT NULL,
                migration_cutoff TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'local',
                reported_post_cutoff_count INTEGER NOT NULL DEFAULT 0,
                evidence_hash TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS decision_observations (
                observation_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                mode TEXT NOT NULL,
                run_id TEXT NOT NULL DEFAULT '',
                as_of TEXT NOT NULL,
                effective_session TEXT NOT NULL,
                history_hash TEXT NOT NULL,
                provider_state_before_hash TEXT NOT NULL,
                provider_state_after_hash TEXT NOT NULL,
                signal_hash TEXT NOT NULL,
                weights_hash TEXT NOT NULL,
                data_version TEXT NOT NULL DEFAULT '',
                model_version TEXT NOT NULL DEFAULT '',
                policy_version TEXT NOT NULL DEFAULT '',
                account_hash TEXT NOT NULL DEFAULT '',
                quote_hash TEXT NOT NULL DEFAULT '',
                instrument_hash TEXT NOT NULL DEFAULT '',
                plan_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(instance_id, config_hash, mode, run_id, as_of)
            );
            CREATE INDEX IF NOT EXISTS ix_decision_observations_compare
                ON decision_observations(instance_id, config_hash, as_of, mode);
            CREATE TABLE IF NOT EXISTS parity_runs (
                parity_run_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                replay_run_id TEXT NOT NULL DEFAULT '',
                shadow_stage_run_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                compared_sessions INTEGER NOT NULL DEFAULT 0,
                pass_count INTEGER NOT NULL DEFAULT 0,
                mismatch_count INTEGER NOT NULL DEFAULT 0,
                not_comparable_count INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS parity_results (
                parity_run_id TEXT NOT NULL,
                session TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                replay_observation_id TEXT NOT NULL DEFAULT '',
                shadow_observation_id TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                PRIMARY KEY (parity_run_id, session),
                FOREIGN KEY (parity_run_id) REFERENCES parity_runs(parity_run_id)
            );
            """,
        )
        _rehash_instances_for_v6(db)
        db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (1, 6, ?)",
            (_now(),),
        )

    @staticmethod
    def _migrate_v7(db: sqlite3.Connection) -> None:
        environment_columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(compatibility_environments)")
        }
        for name, ddl in (
            ("source", "TEXT NOT NULL DEFAULT 'local'"),
            ("reported_post_cutoff_count", "INTEGER NOT NULL DEFAULT 0"),
            ("evidence_hash", "TEXT NOT NULL DEFAULT ''"),
            ("evidence_json", "TEXT NOT NULL DEFAULT '{}'"),
        ):
            if name not in environment_columns:
                db.execute(f"ALTER TABLE compatibility_environments ADD COLUMN {name} {ddl}")
        _execute_script(
            db,
            """
            CREATE TABLE IF NOT EXISTS broker_uat_runs (
                run_id TEXT PRIMARY KEY,
                broker TEXT NOT NULL,
                account_hash TEXT NOT NULL,
                environment TEXT NOT NULL,
                plugin_version TEXT NOT NULL DEFAULT '',
                plugin_hash TEXT NOT NULL DEFAULT '',
                sdk_version TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                symbol TEXT NOT NULL,
                max_notional REAL NOT NULL,
                current_step TEXT NOT NULL DEFAULT '',
                error_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                ended_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS broker_uat_steps (
                run_id TEXT NOT NULL,
                step TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                error_json TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (run_id, step),
                FOREIGN KEY (run_id) REFERENCES broker_uat_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS broker_uat_evidence (
                evidence_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                broker TEXT NOT NULL,
                account_hash TEXT NOT NULL,
                environment TEXT NOT NULL,
                plugin_version TEXT NOT NULL,
                plugin_hash TEXT NOT NULL,
                sdk_version TEXT NOT NULL DEFAULT '',
                capabilities_json TEXT NOT NULL,
                evidence_hash TEXT NOT NULL UNIQUE,
                passed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES broker_uat_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS broker_uat_route_claims (
                run_id TEXT NOT NULL,
                reference TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY (run_id, reference),
                FOREIGN KEY (run_id) REFERENCES broker_uat_runs(run_id)
            );
            CREATE TABLE IF NOT EXISTS qualification_projections (
                instance_id TEXT PRIMARY KEY,
                config_hash TEXT NOT NULL,
                eligible INTEGER NOT NULL,
                projection_json TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE TABLE IF NOT EXISTS legacy_job_imports (
                legacy_job_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL UNIQUE,
                artifact_dir TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            );
            """,
        )
        db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (1, 7, ?)",
            (_now(),),
        )

    @staticmethod
    def _migrate_v8(db: sqlite3.Connection) -> None:
        """Bind Broker UAT evidence to executable artifacts and scenario v2.

        Version 8 is intentionally additive. Existing v7 evidence remains
        readable but cannot satisfy the v2 removal/live gates because it has no
        runtime or native-SDK fingerprint.
        """

        additions = {
            "broker_uat_runs": (
                ("scenario_version", "INTEGER NOT NULL DEFAULT 1"),
                ("code_commit", "TEXT NOT NULL DEFAULT ''"),
                ("runtime_code_hash", "TEXT NOT NULL DEFAULT ''"),
                ("sdk_hash", "TEXT NOT NULL DEFAULT ''"),
                ("requested_notional", "REAL NOT NULL DEFAULT 0"),
                ("filled_notional", "REAL NOT NULL DEFAULT 0"),
            ),
            "broker_uat_evidence": (
                ("scenario_version", "INTEGER NOT NULL DEFAULT 1"),
                ("code_commit", "TEXT NOT NULL DEFAULT ''"),
                ("runtime_code_hash", "TEXT NOT NULL DEFAULT ''"),
                ("sdk_hash", "TEXT NOT NULL DEFAULT ''"),
                ("requested_notional", "REAL NOT NULL DEFAULT 0"),
                ("filled_notional", "REAL NOT NULL DEFAULT 0"),
            ),
            "broker_uat_route_claims": (
                ("notional", "REAL NOT NULL DEFAULT 0"),
            ),
        }
        for table, columns in additions.items():
            existing = {
                str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")
            }
            for name, ddl in columns:
                if name not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        _execute_script(
            db,
            """
            CREATE TABLE IF NOT EXISTS broker_uat_order_events (
                event_hash TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                reference TEXT NOT NULL DEFAULT '',
                order_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                traded REAL NOT NULL DEFAULT 0,
                volume REAL NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                observed_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES broker_uat_runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS ix_broker_uat_order_events_run
                ON broker_uat_order_events(run_id, observed_at);
            """,
        )
        db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (1, 8, ?)",
            (_now(),),
        )

    @staticmethod
    def _migrate_v9(db: sqlite3.Connection) -> None:
        """Add orthogonal execution bindings and external-account namespaces."""

        additions = {
            "deployment_runtime": (
                ("execution_environment", "TEXT NOT NULL DEFAULT 'local_paper'"),
                ("trade_provider", "TEXT NOT NULL DEFAULT 'paper'"),
                ("quote_provider", "TEXT NOT NULL DEFAULT 'paper'"),
                ("account_profile", "TEXT NOT NULL DEFAULT ''"),
                ("quote_data_kind", "TEXT NOT NULL DEFAULT 'synthetic'"),
                ("binding_hash", "TEXT NOT NULL DEFAULT ''"),
                ("reconciled", "INTEGER NOT NULL DEFAULT 0"),
            ),
            "stage_evidence": (
                ("binding_hash", "TEXT NOT NULL DEFAULT ''"),
            ),
            "stage_runs": (
                ("binding_hash", "TEXT NOT NULL DEFAULT ''"),
            ),
        }
        for table, columns in additions.items():
            existing = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns:
                if name not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        _execute_script(
            db,
            """
            CREATE TABLE IF NOT EXISTS execution_bindings (
                instance_id TEXT PRIMARY KEY,
                execution_environment TEXT NOT NULL,
                trade_provider TEXT NOT NULL,
                quote_provider TEXT NOT NULL,
                account_profile TEXT NOT NULL DEFAULT '',
                quote_data_kind TEXT NOT NULL,
                binding_hash TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            """,
        )
        rows = db.execute(
            "SELECT si.instance_id, si.deployment_level, dr.broker "
            "FROM strategy_instances si JOIN deployment_runtime dr "
            "ON dr.instance_id=si.instance_id"
        ).fetchall()
        for row in rows:
            external = str(row["deployment_level"]) in {"shadow", "live"}
            environment = "live" if external else ExecutionEnvironment.LOCAL_PAPER.value
            trade = str(row["broker"] or "").lower() if external else "paper"
            trade = trade or ("unknown" if external else "paper")
            quote = trade if external else "paper"
            data_kind = "realtime" if external else "synthetic"
            binding_hash = _binding_hash(
                str(row["instance_id"]), environment, trade, quote, "", data_kind,
            )
            db.execute(
                "INSERT OR IGNORE INTO execution_bindings "
                "(instance_id, execution_environment, trade_provider, quote_provider, "
                "account_profile, quote_data_kind, binding_hash, updated_at) "
                "VALUES (?, ?, ?, ?, '', ?, ?, ?)",
                (
                    row["instance_id"], environment, trade, quote, data_kind,
                    binding_hash, _now(),
                ),
            )
            db.execute(
                "UPDATE deployment_runtime SET execution_environment=?, trade_provider=?, "
                "quote_provider=?, account_profile='', quote_data_kind=?, binding_hash=?, reconciled=? "
                "WHERE instance_id=?",
                (
                    environment, trade, quote, data_kind, binding_hash,
                    int(not external), row["instance_id"],
                ),
            )
            db.execute(
                "UPDATE stage_evidence SET binding_hash=? "
                "WHERE instance_id=? AND binding_hash=''",
                (binding_hash, row["instance_id"]),
            )
            db.execute(
                "UPDATE stage_runs SET binding_hash=? "
                "WHERE instance_id=? AND binding_hash=''",
                (binding_hash, row["instance_id"]),
            )
        db.execute("DROP INDEX IF EXISTS uq_live_account_writer")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_external_account_writer_v9 "
            "ON deployment_runtime(execution_environment, trade_provider, account_id) "
            "WHERE binding_active=1 AND account_id<>'' "
            "AND execution_environment IN ('live', 'broker_simulation')"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_external_profile_writer_v9 "
            "ON deployment_runtime(execution_environment, trade_provider, account_profile) "
            "WHERE binding_active=1 AND account_profile<>'' "
            "AND execution_environment='broker_simulation'"
        )
        account_blocks = db.execute(
            "SELECT scope_id, active, reason, updated_at FROM route_blocks "
            "WHERE scope_type='account'"
        ).fetchall()
        for block in account_blocks:
            hashed = account_identity_hash(str(block["scope_id"]))
            if hashed == str(block["scope_id"]):
                continue
            db.execute(
                "INSERT INTO route_blocks(scope_type, scope_id, active, reason, updated_at) "
                "VALUES ('account', ?, ?, ?, ?) "
                "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
                "active=MAX(route_blocks.active, excluded.active), "
                "reason=CASE WHEN excluded.active=1 THEN excluded.reason ELSE route_blocks.reason END, "
                "updated_at=MAX(route_blocks.updated_at, excluded.updated_at)",
                (
                    hashed, int(block["active"]), str(block["reason"]),
                    str(block["updated_at"]),
                ),
            )
            db.execute(
                "DELETE FROM route_blocks WHERE scope_type='account' AND scope_id=?",
                (str(block["scope_id"]),),
            )
        db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (1, 9, ?)",
            (_now(),),
        )

    @staticmethod
    def _migrate_v10(db: sqlite3.Connection) -> None:
        """Replace promotion state with independent deployment configuration."""

        _execute_script(
            db,
            """
            DROP INDEX IF EXISTS uq_live_account_writer;
            DROP INDEX IF EXISTS uq_external_account_writer_v9;
            DROP INDEX IF EXISTS uq_external_profile_writer_v9;
            DROP TABLE IF EXISTS parity_results;
            DROP TABLE IF EXISTS parity_runs;
            DROP TABLE IF EXISTS qualification_projections;
            DROP TABLE IF EXISTS stage_run_events;
            DROP TABLE IF EXISTS stage_run_sessions;
            DROP TABLE IF EXISTS stage_runs;
            DROP TABLE IF EXISTS stage_evidence;
            DROP TABLE IF EXISTS deployment_events;
            DROP TABLE IF EXISTS live_approvals;
            DROP TABLE IF EXISTS account_baselines;
            DROP TABLE IF EXISTS execution_bindings;
            DROP TABLE IF EXISTS deployment_runtime;
            """,
        )
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(strategy_instances)")
        }
        if "lifecycle" in columns:
            db.execute(
                "ALTER TABLE strategy_instances RENAME COLUMN lifecycle TO validation_state"
            )
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(strategy_instances)")
        }
        if "deployment_level" in columns:
            db.execute("ALTER TABLE strategy_instances DROP COLUMN deployment_level")
        _execute_script(
            db,
            """
            CREATE TABLE deployment_specs (
                instance_id TEXT PRIMARY KEY,
                config_hash TEXT NOT NULL,
                run_mode TEXT NOT NULL,
                execution_environment TEXT NOT NULL,
                trade_provider TEXT NOT NULL,
                quote_provider TEXT NOT NULL,
                account_profile TEXT NOT NULL DEFAULT '',
                account_id TEXT NOT NULL DEFAULT '',
                quote_data_kind TEXT NOT NULL,
                binding_hash TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE TABLE deployment_runtime (
                instance_id TEXT PRIMARY KEY,
                config_hash TEXT NOT NULL,
                binding_hash TEXT NOT NULL,
                run_mode TEXT NOT NULL,
                account_id TEXT NOT NULL DEFAULT '',
                execution_environment TEXT NOT NULL,
                trade_provider TEXT NOT NULL,
                quote_provider TEXT NOT NULL,
                account_profile TEXT NOT NULL DEFAULT '',
                quote_data_kind TEXT NOT NULL,
                desired_state TEXT NOT NULL,
                observed_state TEXT NOT NULL,
                runtime_id TEXT NOT NULL DEFAULT '',
                runner_heartbeat_at TEXT NOT NULL DEFAULT '',
                last_command_id TEXT NOT NULL DEFAULT '',
                last_error_json TEXT NOT NULL DEFAULT '{}',
                reconcile_required INTEGER NOT NULL DEFAULT 0,
                reconciled INTEGER NOT NULL DEFAULT 0,
                binding_active INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES deployment_specs(instance_id)
            );
            CREATE UNIQUE INDEX uq_external_account_writer_v10
                ON deployment_runtime(execution_environment, trade_provider, account_id)
                WHERE binding_active=1 AND account_id<>''
                AND execution_environment IN ('live', 'broker_simulation');
            CREATE UNIQUE INDEX uq_external_profile_writer_v10
                ON deployment_runtime(execution_environment, trade_provider, account_profile)
                WHERE binding_active=1 AND account_profile<>''
                AND execution_environment='broker_simulation';
            CREATE TABLE runtime_runs (
                run_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                run_mode TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                binding_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL DEFAULT '',
                trading_sessions INTEGER NOT NULL DEFAULT 0,
                metrics_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE INDEX ix_runtime_runs_instance_mode
                ON runtime_runs(instance_id, run_mode, config_hash);
            CREATE UNIQUE INDEX uq_active_runtime_run_v10
                ON runtime_runs(instance_id) WHERE status='running';
            CREATE TABLE runtime_run_sessions (
                run_id TEXT NOT NULL,
                session TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (run_id, session),
                FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id)
            );
            CREATE TABLE runtime_run_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 1,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id)
            );
            CREATE TABLE decision_comparisons (
                comparison_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                left_mode TEXT NOT NULL,
                left_run_id TEXT NOT NULL,
                right_mode TEXT NOT NULL,
                right_run_id TEXT NOT NULL,
                status TEXT NOT NULL,
                compared_sessions INTEGER NOT NULL DEFAULT 0,
                match_count INTEGER NOT NULL DEFAULT 0,
                mismatch_count INTEGER NOT NULL DEFAULT 0,
                not_comparable_count INTEGER NOT NULL DEFAULT 0,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES strategy_instances(instance_id)
            );
            CREATE TABLE decision_comparison_results (
                comparison_id TEXT NOT NULL,
                session TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                left_observation_id TEXT NOT NULL DEFAULT '',
                right_observation_id TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                PRIMARY KEY (comparison_id, session),
                FOREIGN KEY (comparison_id) REFERENCES decision_comparisons(comparison_id)
            );
            """,
        )
        db.execute(
            "INSERT OR REPLACE INTO schema_version VALUES (1, 10, ?)",
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
                "INSERT INTO strategy_instances "
                "(instance_id, strategy_id, strategy_version, config_json, config_hash, "
                "validation_state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    config.instance_id, config.strategy_id, config.strategy_version,
                    _json(config.to_dict()), config.config_hash,
                    InstanceValidationState.CREATED.value, now,
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

    def get_deployment_spec(self, instance_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM deployment_specs WHERE instance_id=?", (instance_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"deployment is not configured for {instance_id!r}")
        current = self.get_instance(instance_id)
        return {**_deployment_spec_row(row), "stale": row["config_hash"] != current["config_hash"]}

    def list_deployments(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT instance_id FROM deployment_specs ORDER BY instance_id").fetchall()
        return [self.deployment(str(row["instance_id"])) for row in rows]

    def configure_deployment(self, spec: DeploymentSpec) -> dict[str, Any]:
        current = self.get_instance(spec.instance_id)
        if current["validation_state"] != InstanceValidationState.VALIDATED.value:
            raise ValueError("strategy instance must be validated before deployment")
        if spec.config_hash != current["config_hash"]:
            raise ValueError("deployment spec must bind the current config_hash")
        now = _now()
        with self._lock, self._connect() as db:
            # Serialize the validation/config check with both spec and runtime
            # writes.  A second Portal process must not be able to change the
            # instance between the read above and this deployment replacement.
            db.execute("BEGIN IMMEDIATE")
            persisted_instance = db.execute(
                "SELECT config_hash, validation_state FROM strategy_instances "
                "WHERE instance_id=?",
                (spec.instance_id,),
            ).fetchone()
            if persisted_instance is None:
                raise KeyError(f"unknown strategy instance {spec.instance_id!r}")
            if str(persisted_instance["config_hash"]) != spec.config_hash:
                raise RuntimeError("strategy instance changed concurrently during deployment")
            if (
                str(persisted_instance["validation_state"])
                != InstanceValidationState.VALIDATED.value
            ):
                raise ValueError("strategy instance must be validated before deployment")
            persisted = db.execute(
                "SELECT * FROM deployment_specs WHERE instance_id=?", (spec.instance_id,)
            ).fetchone()
            runtime = db.execute(
                "SELECT * FROM deployment_runtime WHERE instance_id=?", (spec.instance_id,)
            ).fetchone()
            if runtime is not None and (
                str(runtime["runtime_id"])
                or str(runtime["desired_state"]) not in {"ready", "stopped", "error"}
                or str(runtime["observed_state"]) not in {"ready", "stopped", "error"}
            ):
                raise ValueError("stop the strategy daemon before changing deployment")
            unchanged = persisted is not None and all(
                str(persisted[name]) == str(getattr(spec, name))
                for name in (
                    "config_hash", "run_mode", "execution_environment", "trade_provider",
                    "quote_provider", "account_profile", "account_id", "quote_data_kind",
                    "binding_hash",
                )
            )
            route_block = db.execute(
                "SELECT active FROM route_blocks WHERE scope_type='runtime' AND scope_id=?",
                (spec.instance_id,),
            ).fetchone()
            runtime_is_initial = runtime is not None and all((
                str(runtime["config_hash"]) == spec.config_hash,
                str(runtime["binding_hash"]) == spec.binding_hash,
                str(runtime["run_mode"]) == spec.run_mode,
                str(runtime["account_id"]) == spec.account_id,
                str(runtime["trade_provider"]) == spec.trade_provider,
                str(runtime["quote_provider"]) == spec.quote_provider,
                str(runtime["account_profile"]) == spec.account_profile,
                str(runtime["desired_state"]) == LifecycleState.READY.value,
                str(runtime["observed_state"]) == LifecycleState.READY.value,
                not str(runtime["runtime_id"]),
                not bool(runtime["binding_active"]),
                not bool(runtime["reconcile_required"]),
                bool(runtime["reconciled"]) == (spec.run_mode == DeploymentMode.PAPER.value),
            ))
            if unchanged and runtime_is_initial and not bool(
                route_block is not None and route_block["active"]
            ):
                return self.deployment(spec.instance_id)
            version = (
                1 if persisted is None
                else int(persisted["version"]) if unchanged
                else int(persisted["version"]) + 1
            )
            db.execute(
                "INSERT INTO deployment_specs "
                "(instance_id,config_hash,run_mode,execution_environment,trade_provider,"
                "quote_provider,account_profile,account_id,quote_data_kind,binding_hash,version,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instance_id) DO UPDATE SET "
                "config_hash=excluded.config_hash,run_mode=excluded.run_mode,"
                "execution_environment=excluded.execution_environment,trade_provider=excluded.trade_provider,"
                "quote_provider=excluded.quote_provider,account_profile=excluded.account_profile,"
                "account_id=excluded.account_id,quote_data_kind=excluded.quote_data_kind,"
                "binding_hash=excluded.binding_hash,version=excluded.version,updated_at=excluded.updated_at",
                (
                    spec.instance_id, spec.config_hash, spec.run_mode, spec.execution_environment,
                    spec.trade_provider, spec.quote_provider, spec.account_profile, spec.account_id,
                    spec.quote_data_kind, spec.binding_hash, version, now,
                ),
            )
            db.execute(
                "INSERT INTO deployment_runtime "
                "(instance_id,config_hash,binding_hash,run_mode,account_id,execution_environment,"
                "trade_provider,quote_provider,account_profile,quote_data_kind,desired_state,"
                "observed_state,reconciled,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(instance_id) DO UPDATE SET config_hash=excluded.config_hash,"
                "binding_hash=excluded.binding_hash,run_mode=excluded.run_mode,"
                "account_id=excluded.account_id,execution_environment=excluded.execution_environment,"
                "trade_provider=excluded.trade_provider,quote_provider=excluded.quote_provider,"
                "account_profile=excluded.account_profile,quote_data_kind=excluded.quote_data_kind,"
                "desired_state='ready',observed_state='ready',runtime_id='',runner_heartbeat_at='',"
                "last_command_id='',last_error_json='{}',reconcile_required=0,reconciled=excluded.reconciled,"
                "binding_active=0,version=deployment_runtime.version+1,updated_at=excluded.updated_at",
                (
                    spec.instance_id, spec.config_hash, spec.binding_hash, spec.run_mode, spec.account_id,
                    spec.execution_environment, spec.trade_provider, spec.quote_provider,
                    spec.account_profile, spec.quote_data_kind, LifecycleState.READY.value,
                    LifecycleState.READY.value,
                    int(spec.run_mode == DeploymentMode.PAPER.value), now,
                ),
            )
            db.execute(
                "UPDATE route_blocks SET active=0,reason='',updated_at=? "
                "WHERE scope_type='runtime' AND scope_id=?",
                (now, spec.instance_id),
            )
        return self.deployment(spec.instance_id)

    def update_instance(self, instance_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        current = self.get_instance(instance_id)
        payload = dict(current["config"])
        allowed = {
            "params", "universe", "frequency", "data_policy", "portfolio_policy",
            "strategy_version", "strategy_code_hash", "model_hash", "artifact_binding",
        }
        payload.update({key: value for key, value in changes.items() if key in allowed})
        payload["config_hash"] = ""
        config = StrategyInstanceConfig.from_dict(payload)
        changed = config.config_hash != current["config_hash"]
        validation_state = (
            InstanceValidationState.CREATED.value
            if changed else current["validation_state"]
        )
        with self._lock, self._connect() as db:
            persisted = db.execute(
                "SELECT config_hash FROM strategy_instances WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if persisted is None:
                raise KeyError(f"unknown strategy instance {instance_id!r}")
            if persisted["config_hash"] != current["config_hash"]:
                raise RuntimeError("strategy instance changed concurrently during update")
            db.execute(
                "UPDATE strategy_instances SET strategy_version=?, config_json=?, config_hash=?, "
                "validation_state=?, updated_at=? WHERE instance_id=?",
                (
                    config.strategy_version, _json(config.to_dict()), config.config_hash,
                    validation_state, _now(), instance_id,
                ),
            )
            if changed:
                db.execute(
                    "UPDATE runtime_runs SET status='invalidated', ended_at=CASE "
                    "WHEN ended_at='' THEN ? ELSE ended_at END, updated_at=? "
                    "WHERE instance_id=? AND status='running'",
                    (_now(), _now(), instance_id),
                )
                db.execute(
                    "UPDATE deployment_runtime SET desired_state='stopped', observed_state='stopped', "
                    "runtime_id='', runner_heartbeat_at='', last_command_id='', last_error_json='{}', "
                    "reconcile_required=1, reconciled=0, "
                    "binding_active=0, version=version+1, updated_at=? WHERE instance_id=?",
                    (_now(), instance_id),
                )
                db.execute(
                    "INSERT INTO route_blocks(scope_type,scope_id,active,reason,updated_at) "
                    "VALUES ('runtime',?,1,'deployment is stale after strategy config change',?) "
                    "ON CONFLICT(scope_type,scope_id) DO UPDATE SET active=1,"
                    "reason=excluded.reason,updated_at=excluded.updated_at",
                    (instance_id, _now()),
                )
        return self.get_instance(instance_id)

    def set_validation_state(self, instance_id: str, state: str) -> dict[str, Any]:
        value = InstanceValidationState(state).value
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE strategy_instances SET validation_state=?, updated_at=? WHERE instance_id=?",
                (value, _now(), instance_id),
            )
        return self.get_instance(instance_id)

    def deployment(self, instance_id: str) -> dict[str, Any]:
        current = self.get_instance(instance_id)
        return {
            "instance": current,
            "configuration": self.get_deployment_spec(instance_id),
            "runtime": self.get_runtime_state(instance_id),
            "runs": self.list_runtime_runs(instance_id),
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
            "config_hash", "binding_hash", "run_mode", "account_id",
            "desired_state", "observed_state", "runtime_id", "runner_heartbeat_at",
            "last_command_id", "last_error", "reconcile_required", "binding_active",
            "execution_environment", "trade_provider", "quote_provider",
            "account_profile", "quote_data_kind", "reconciled",
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
            if key == "account_id":
                value = account_identity_hash(str(value or ""))
            if key in {"reconcile_required", "binding_active", "reconciled"}:
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
            raise ValueError("external account already has an active automated writer") from exc
        return self.get_runtime_state(instance_id)

    def transition_runtime(
        self,
        instance_id: str,
        *,
        lifecycle: str,
        expected_version: int | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        """Atomically update the deployment runtime projection."""

        lifecycle_value = LifecycleState(lifecycle).value
        changes.setdefault("observed_state", lifecycle_value)
        allowed = {
            "config_hash", "binding_hash", "run_mode", "account_id",
            "desired_state", "observed_state", "runtime_id", "runner_heartbeat_at",
            "last_command_id", "last_error", "reconcile_required", "binding_active",
            "execution_environment", "trade_provider", "quote_provider",
            "account_profile", "quote_data_kind", "reconciled",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unsupported runtime fields: {sorted(unknown)}")
        values: dict[str, Any] = {}
        for key, value in changes.items():
            column = "last_error_json" if key == "last_error" else key
            if key == "last_error":
                value = _json(value or {})
            if key == "account_id":
                value = account_identity_hash(str(value or ""))
            if key in {"reconcile_required", "binding_active", "reconciled"}:
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
        try:
            with self._lock, self._connect() as db:
                cursor = db.execute(runtime_sql, params)
                if cursor.rowcount != 1:
                    raise RuntimeError("deployment runtime changed concurrently")
        except sqlite3.IntegrityError as exc:
            raise ValueError("external account already has an active automated writer") from exc
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
        if scope not in {"global", "account", "instance", "runtime"}:
            raise ValueError("scope_type must be global, account, instance or runtime")
        if scope == "global":
            identifier = "*"
        elif scope == "account":
            identifier = account_identity_hash(identifier)
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
        account_hash = account_identity_hash(account_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM route_blocks WHERE active=1 AND "
                "((scope_type='global' AND scope_id='*') OR "
                "(scope_type='instance' AND scope_id=?) OR "
                "(scope_type='runtime' AND scope_id=?) OR "
                "(scope_type='account' AND scope_id IN (?, ?))) "
                "ORDER BY scope_type, scope_id",
                (instance_id, instance_id, account_id, account_hash),
            ).fetchall()
        return [_route_block_row(row) for row in rows]

    def active_live_writer(self, account_id: str) -> dict[str, Any] | None:
        identity = account_identity_hash(account_id)
        if not identity:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM deployment_runtime WHERE account_id=? "
                "AND run_mode='live' AND binding_active=1 LIMIT 1",
                (identity,),
            ).fetchone()
        return None if row is None else _runtime_row(row)

    def active_external_writer(
        self,
        *,
        execution_environment: str,
        trade_provider: str,
        account_id: str = "",
        account_profile: str = "",
    ) -> dict[str, Any] | None:
        """Return the single writer for a live/simulation account identity."""

        environment = str(execution_environment).strip().lower()
        provider = str(trade_provider).strip().lower()
        if environment not in {
            ExecutionEnvironment.BROKER_SIMULATION.value,
            ExecutionEnvironment.LIVE.value,
        }:
            return None
        identity_column = "account_id" if str(account_id).strip() else "account_profile"
        identity = (
            account_identity_hash(account_id)
            if identity_column == "account_id"
            else str(account_profile).strip()
        )
        if not identity:
            return None
        with self._connect() as db:
            row = db.execute(
                f"SELECT * FROM deployment_runtime WHERE execution_environment=? "
                f"AND trade_provider=? AND {identity_column}=? AND binding_active=1 LIMIT 1",
                (environment, provider, identity),
            ).fetchone()
        return None if row is None else _runtime_row(row)

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
                account_hash = account_identity_hash(account_id)
                rows = db.execute(
                    "SELECT * FROM route_blocks WHERE scope_type='global' OR "
                    "(scope_type='instance' AND scope_id=?) OR "
                    "(scope_type='runtime' AND scope_id=?) OR "
                    "(scope_type='account' AND scope_id IN (?,?)) "
                    "ORDER BY scope_type, scope_id",
                    (instance_id, instance_id, account_id, account_hash),
                ).fetchall()
        return [_route_block_row(row) for row in rows]

    def start_runtime_run(
        self,
        instance_id: str,
        run_mode: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        mode = DeploymentMode(run_mode).value
        current = self.get_instance(instance_id)
        deployment = self.get_deployment_spec(instance_id)
        if deployment["stale"]:
            raise ValueError("deployment is stale; configure it for the current strategy")
        if deployment["run_mode"] != mode:
            raise ValueError(
                f"deployment is configured for {deployment['run_mode']}, not {mode}"
            )
        identifier = str(run_id or uuid.uuid4().hex)
        now = _now()
        with self._lock, self._connect() as db:
            persisted = db.execute(
                "SELECT config_hash, run_mode, binding_hash FROM deployment_specs "
                "WHERE instance_id=?",
                (instance_id,),
            ).fetchone()
            if persisted is None:
                raise KeyError(f"deployment is not configured for {instance_id!r}")
            if (
                str(persisted["config_hash"]) != current["config_hash"]
                or str(persisted["run_mode"]) != mode
                or str(persisted["binding_hash"]) != deployment["binding_hash"]
            ):
                raise RuntimeError("deployment changed concurrently before runtime run start")
            try:
                db.execute(
                    "INSERT INTO runtime_runs "
                    "(run_id,instance_id,run_mode,config_hash,binding_hash,status,started_at,updated_at) "
                    "VALUES (?,?,?,?,?,'running',?,?)",
                    (
                        identifier, instance_id, mode, current["config_hash"],
                        deployment["binding_hash"], now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(
                    f"strategy instance {instance_id!r} already has an active runtime run"
                ) from exc
        return self.get_runtime_run(identifier)

    def get_active_runtime_run(
        self,
        instance_id: str,
        *,
        run_mode: str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_instance(instance_id)
        deployment = self.get_deployment_spec(instance_id)
        mode = deployment["run_mode"] if run_mode is None else DeploymentMode(run_mode).value
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM runtime_runs WHERE instance_id=? AND run_mode=? "
                "AND config_hash=? AND binding_hash=? AND status='running' "
                "ORDER BY started_at DESC LIMIT 1",
                (
                    instance_id, mode, current["config_hash"],
                    deployment["binding_hash"],
                ),
            ).fetchone()
        return None if row is None else _runtime_run_row(row)

    def record_runtime_session(
        self,
        instance_id: str,
        *,
        config_hash: str,
        run_mode: str,
        session: str,
    ) -> bool:
        mode = DeploymentMode(run_mode).value
        deployment = self.get_deployment_spec(instance_id)
        with self._lock, self._connect() as db:
            run = db.execute(
                "SELECT run_id FROM runtime_runs WHERE instance_id=? AND run_mode=? "
                "AND config_hash=? AND binding_hash=? AND status='running' "
                "ORDER BY started_at DESC LIMIT 1",
                (instance_id, mode, config_hash, deployment["binding_hash"]),
            ).fetchone()
            if run is None:
                return False
            cursor = db.execute(
                "INSERT OR IGNORE INTO runtime_run_sessions VALUES (?,?,?)",
                (run["run_id"], str(session)[:10], _now()),
            )
            db.execute(
                "UPDATE runtime_runs SET trading_sessions=(SELECT COUNT(*) "
                "FROM runtime_run_sessions WHERE run_id=?),updated_at=? WHERE run_id=?",
                (run["run_id"], _now(), run["run_id"]),
            )
            return cursor.rowcount == 1

    def record_runtime_event(
        self,
        instance_id: str,
        *,
        config_hash: str,
        run_mode: str,
        event_type: str,
        count: int = 1,
        details: Any = None,
    ) -> bool:
        mode = DeploymentMode(run_mode).value
        deployment = self.get_deployment_spec(instance_id)
        with self._lock, self._connect() as db:
            run = db.execute(
                "SELECT run_id FROM runtime_runs WHERE instance_id=? AND run_mode=? "
                "AND config_hash=? AND binding_hash=? AND status='running' "
                "ORDER BY started_at DESC LIMIT 1",
                (instance_id, mode, config_hash, deployment["binding_hash"]),
            ).fetchone()
            if run is None:
                return False
            db.execute(
                "INSERT INTO runtime_run_events "
                "(run_id,event_type,count,details_json,created_at) VALUES (?,?,?,?,?)",
                (
                    run["run_id"], str(event_type), max(int(count), 0),
                    _json(details or {}), _now(),
                ),
            )
        return True

    def finish_runtime_run(
        self,
        run_id: str,
        *,
        trading_sessions: int = 0,
        metrics: dict[str, Any] | None = None,
        status: str = "completed",
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled", "invalidated"}:
            raise ValueError("runtime run status must be completed, failed, cancelled or invalidated")
        with self._lock, self._connect() as db:
            run = db.execute(
                "SELECT * FROM runtime_runs WHERE run_id=? AND status='running'", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError(f"runtime run {run_id!r} is missing or already terminal")
            event_rows = db.execute(
                "SELECT event_type,SUM(count) AS total FROM runtime_run_events "
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
            if str(run["run_mode"]) == DeploymentMode.LIVE.value and status == "completed":
                combined.update(_live_execution_quality_metrics(db, run))
            recorded_sessions = int(db.execute(
                "SELECT COUNT(*) FROM runtime_run_sessions WHERE run_id=?", (run_id,)
            ).fetchone()[0])
            declared_sessions = max(int(trading_sessions), 0)
            if declared_sessions != recorded_sessions:
                combined["declared_trading_sessions"] = declared_sessions
            cursor = db.execute(
                "UPDATE runtime_runs SET status=?,ended_at=?,trading_sessions=?,metrics_json=?,"
                "updated_at=? WHERE run_id=? AND status='running'",
                (status, _now(), recorded_sessions, _json(combined), _now(), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"runtime run {run_id!r} is missing or already terminal")
        return self.get_runtime_run(run_id)

    def get_runtime_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runtime_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown runtime run {run_id!r}")
        return _runtime_run_row(row)

    def list_runtime_runs(self, instance_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runtime_runs WHERE instance_id=? ORDER BY started_at,run_id",
                (instance_id,),
            ).fetchall()
        return [_runtime_run_row(row) for row in rows]

    def list_runtime_sessions(
        self,
        instance_id: str,
        run_mode: str,
        *,
        config_hash: str | None = None,
        binding_hash: str | None = None,
        started_after: str = "",
    ) -> list[str]:
        mode = DeploymentMode(run_mode).value
        current = self.get_instance(instance_id)
        deployment = self.get_deployment_spec(instance_id)
        clauses = [
            "r.instance_id=?", "r.run_mode=?", "r.config_hash=?",
            "r.status='completed'",
        ]
        values: list[Any] = [
            instance_id, mode, config_hash or current["config_hash"],
        ]
        if binding_hash:
            clauses.append("r.binding_hash=?")
            values.append(binding_hash)
        if started_after:
            clauses.append("r.started_at>=?")
            values.append(str(started_after))
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT s.session FROM runtime_run_sessions s "
                "JOIN runtime_runs r ON r.run_id=s.run_id WHERE "
                + " AND ".join(clauses) + " ORDER BY s.session",
                values,
            ).fetchall()
        return [str(row["session"]) for row in rows]

    def runtime_diagnostics(self, instance_id: str) -> dict[str, Any]:
        current = self.get_instance(instance_id)
        runs = self.list_runtime_runs(instance_id)
        metric_names = (
            "unresolved_errors", "duplicate_routes", "position_breaches",
            "reconciliation_warnings",
        )
        summaries: dict[str, Any] = {}
        for mode in DeploymentMode:
            selected = [
                run for run in runs
                if run["run_mode"] == mode.value
                and run["config_hash"] == current["config_hash"]
            ]
            run_ids = [run["run_id"] for run in selected]
            sessions = 0
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                with self._connect() as db:
                    sessions = int(db.execute(
                        f"SELECT COUNT(DISTINCT session) FROM runtime_run_sessions "
                        f"WHERE run_id IN ({placeholders})", run_ids,
                    ).fetchone()[0])
            failures = {
                name: sum(_metric_count(run["metrics"].get(name)) for run in selected)
                for name in metric_names
            }
            quality = [
                {
                    "run_id": run["run_id"],
                    "order_count": int(run["metrics"].get("execution_quality_order_count") or 0),
                    "median_bp": _optional_float(
                        run["metrics"].get("median_implementation_shortfall_bp")
                    ),
                    "p95_bp": _optional_float(
                        run["metrics"].get("p95_implementation_shortfall_bp")
                    ),
                }
                for run in selected
                if mode == DeploymentMode.LIVE
            ]
            summaries[mode.value] = {
                "run_count": len(selected),
                "completed_runs": sum(run["status"] == "completed" for run in selected),
                "trading_sessions": sessions,
                "failures": failures,
                "execution_quality": quality,
            }
        return {
            "instance_id": instance_id,
            "config_hash": current["config_hash"],
            "modes": summaries,
            "runs": runs,
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

    def get_child_order(self, reference: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM child_orders WHERE reference=?", (reference,)
            ).fetchone()
        if row is None:
            return None
        return {
            **dict(row),
            "payload": json.loads(row["payload_json"] or "{}"),
        }

    # ---- immutable artifacts, signals and decisions --------------------- #

    def record_artifact_manifest(
        self,
        instance_id: str,
        config_hash: str,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        artifact_id = hashlib.sha256(
            _json({"instance_id": instance_id, "config_hash": config_hash, "manifest": manifest}).encode()
        ).hexdigest()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO artifact_manifests VALUES (?, ?, ?, ?, ?, ?)",
                (
                    artifact_id, instance_id, config_hash,
                    str(manifest.get("artifact_type") or "unknown"), _json(manifest), _now(),
                ),
            )
            row = db.execute(
                "SELECT * FROM artifact_manifests WHERE instance_id=? AND config_hash=?",
                (instance_id, config_hash),
            ).fetchone()
        assert row is not None
        return _artifact_row(row)

    def get_artifact_manifest(self, instance_id: str, config_hash: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM artifact_manifests WHERE instance_id=? AND config_hash=?",
                (instance_id, config_hash),
            ).fetchone()
        return None if row is None else _artifact_row(row)

    def record_signal_envelope(
        self,
        signal_id: str,
        instance_id: str,
        config_hash: str,
        *,
        as_of: str,
        signal_kind: str,
        payload: Any,
    ) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO signal_envelopes VALUES (?, ?, ?, ?, ?, ?, ?)",
                (signal_id, instance_id, config_hash, as_of, signal_kind, _json(payload), _now()),
            )
            return cursor.rowcount == 1

    def record_portfolio_decision(self, payload: dict[str, Any]) -> bool:
        decision_id = str(payload.get("decision_id") or "")
        if not decision_id:
            raise ValueError("decision_id is required")
        now = _now()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO portfolio_decisions "
                "(decision_id, instance_id, config_hash, as_of, effective_session, valid_until, "
                "status, payload_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id, str(payload.get("instance_id") or ""),
                    str(payload.get("config_hash") or ""), str(payload.get("as_of") or ""),
                    str(payload.get("effective_session") or ""), str(payload.get("valid_until") or ""),
                    "pending", _json(payload), now, now,
                ),
            )
            if cursor.rowcount:
                db.execute(
                    "INSERT OR IGNORE INTO decisions VALUES (?, ?, ?, ?, ?)",
                    (
                        decision_id, str(payload.get("instance_id") or ""),
                        str(payload.get("config_hash") or ""), _json(payload), now,
                    ),
                )
            return cursor.rowcount == 1

    def get_portfolio_decision(self, decision_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM portfolio_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown portfolio decision {decision_id!r}")
        return _portfolio_decision_row(row)

    def list_effective_decisions(
        self,
        instance_id: str,
        session: str,
        *,
        status: str = "pending",
    ) -> list[dict[str, Any]]:
        current = self.get_instance(instance_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM portfolio_decisions WHERE instance_id=? AND config_hash=? "
                "AND effective_session=? AND status=? ORDER BY created_at, decision_id",
                (instance_id, current["config_hash"], session, status),
            ).fetchall()
        return [_portfolio_decision_row(row) for row in rows]

    def list_due_decisions(
        self,
        instance_id: str,
        effective_at: str,
        *,
        status: str = "pending",
    ) -> list[dict[str, Any]]:
        """Return immutable decisions whose effective timestamp has arrived."""

        current = self.get_instance(instance_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM portfolio_decisions WHERE instance_id=? AND config_hash=? "
                "AND effective_session<=? AND status=? "
                "ORDER BY effective_session, created_at, decision_id",
                (instance_id, current["config_hash"], str(effective_at), status),
            ).fetchall()
        return [_portfolio_decision_row(row) for row in rows]

    def update_decision_status(self, decision_id: str, status: str) -> dict[str, Any]:
        if status not in {"pending", "sizing", "planned", "completed", "expired", "blocked"}:
            raise ValueError("unsupported portfolio decision status")
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE portfolio_decisions SET status=?, updated_at=? WHERE decision_id=?",
                (status, _now(), decision_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown portfolio decision {decision_id!r}")
        return self.get_portfolio_decision(decision_id)

    def save_provider_checkpoint(
        self,
        instance_id: str,
        config_hash: str,
        *,
        state_schema_version: int,
        state: dict[str, Any],
        last_evaluated_as_of: str = "",
        state_hash: str = "",
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO provider_checkpoints "
                "(instance_id, config_hash, state_schema_version, state_json, updated_at, "
                "last_evaluated_as_of, state_hash) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance_id, config_hash) DO UPDATE SET "
                "state_schema_version=excluded.state_schema_version, state_json=excluded.state_json, "
                "updated_at=excluded.updated_at, "
                "last_evaluated_as_of=excluded.last_evaluated_as_of, "
                "state_hash=excluded.state_hash",
                (
                    instance_id, config_hash, int(state_schema_version), _json(state), _now(),
                    str(last_evaluated_as_of), str(state_hash),
                ),
            )

    def load_provider_checkpoint(self, instance_id: str, config_hash: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM provider_checkpoints WHERE instance_id=? AND config_hash=?",
                (instance_id, config_hash),
            ).fetchone()
        if row is None:
            return None
        return {
            "instance_id": row["instance_id"],
            "config_hash": row["config_hash"],
            "state_schema_version": int(row["state_schema_version"]),
            "state": json.loads(row["state_json"] or "{}"),
            "updated_at": row["updated_at"],
            "last_evaluated_as_of": row["last_evaluated_as_of"],
            "state_hash": row["state_hash"],
        }

    # ---- deterministic decision observations and comparisons ----------- #

    def record_decision_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        observation_id = str(payload.get("observation_id") or uuid.uuid4().hex)
        now = _now()
        values = (
            observation_id,
            str(payload.get("decision_id") or ""),
            str(payload.get("instance_id") or ""),
            str(payload.get("config_hash") or ""),
            str(payload.get("mode") or "unknown"),
            str(payload.get("run_id") or ""),
            str(payload.get("as_of") or ""),
            str(payload.get("effective_session") or ""),
            str(payload.get("history_hash") or ""),
            str(payload.get("provider_state_before_hash") or ""),
            str(payload.get("provider_state_after_hash") or ""),
            str(payload.get("signal_hash") or ""),
            str(payload.get("weights_hash") or ""),
            str(payload.get("data_version") or ""),
            str(payload.get("model_version") or ""),
            str(payload.get("policy_version") or ""),
            str(payload.get("account_hash") or ""),
            str(payload.get("quote_hash") or ""),
            str(payload.get("instrument_hash") or ""),
            str(payload.get("plan_hash") or ""),
            now,
        )
        with self._lock, self._connect() as db:
            existing = db.execute(
                "SELECT * FROM decision_observations WHERE instance_id=? AND config_hash=? "
                "AND mode=? AND run_id=? AND as_of=?",
                (values[2], values[3], values[4], values[5], values[6]),
            ).fetchone()
            if existing is not None:
                immutable_fields = (
                    "decision_id", "effective_session", "history_hash",
                    "provider_state_before_hash", "provider_state_after_hash",
                    "signal_hash", "weights_hash", "data_version", "model_version",
                    "policy_version",
                )
                incoming = {
                    "decision_id": values[1],
                    "effective_session": values[7],
                    "history_hash": values[8],
                    "provider_state_before_hash": values[9],
                    "provider_state_after_hash": values[10],
                    "signal_hash": values[11],
                    "weights_hash": values[12],
                    "data_version": values[13],
                    "model_version": values[14],
                    "policy_version": values[15],
                }
                changed = {
                    field: {"persisted": str(existing[field]), "incoming": str(incoming[field])}
                    for field in immutable_fields
                    if str(existing[field]) != str(incoming[field])
                }
                if changed:
                    raise ValueError(
                        "decision observation is immutable for a mode/run/as_of; "
                        f"changed fields: {sorted(changed)}"
                    )
            db.execute(
                "INSERT INTO decision_observations VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance_id, config_hash, mode, run_id, as_of) DO UPDATE SET "
                "account_hash=excluded.account_hash, "
                "quote_hash=excluded.quote_hash, instrument_hash=excluded.instrument_hash, "
                "plan_hash=excluded.plan_hash, created_at=excluded.created_at",
                values,
            )
            row = db.execute(
                "SELECT * FROM decision_observations WHERE instance_id=? AND config_hash=? "
                "AND mode=? AND run_id=? AND as_of=?",
                (values[2], values[3], values[4], values[5], values[6]),
            ).fetchone()
        assert row is not None
        return dict(row)

    def find_decision_observation(
        self,
        instance_id: str,
        config_hash: str,
        *,
        mode: str,
        run_id: str,
        as_of: str,
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM decision_observations WHERE instance_id=? AND config_hash=? "
                "AND mode=? AND run_id=? AND as_of=?",
                (instance_id, config_hash, mode, run_id, as_of),
            ).fetchone()
        return None if row is None else dict(row)

    def find_mode_observations_for_as_of(
        self,
        instance_id: str,
        config_hash: str,
        *,
        mode: str,
        as_of: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM decision_observations WHERE instance_id=? AND config_hash=? "
                "AND mode=? AND as_of=? ORDER BY created_at, observation_id",
                (instance_id, config_hash, str(mode), str(as_of)),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_decision_observations(
        self,
        instance_id: str,
        *,
        mode: str | None = None,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["instance_id=?"]
        values: list[Any] = [instance_id]
        if mode is not None:
            clauses.append("mode=?")
            values.append(str(mode))
        if run_id is not None:
            clauses.append("run_id=?")
            values.append(str(run_id))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM decision_observations WHERE " + " AND ".join(clauses)
                + " ORDER BY as_of, observation_id",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def attach_execution_observation(
        self,
        instance_id: str,
        config_hash: str,
        *,
        mode: str,
        run_id: str,
        as_of: str,
        account_hash: str,
        quote_hash: str,
        instrument_hash: str,
        plan_hash: str,
    ) -> dict[str, Any]:
        execution_hashes = {
            "account_hash": str(account_hash),
            "quote_hash": str(quote_hash),
            "instrument_hash": str(instrument_hash),
            "plan_hash": str(plan_hash),
        }
        if not all(execution_hashes.values()):
            raise ValueError("execution observation hashes must all be present")
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM decision_observations WHERE instance_id=? AND config_hash=? "
                "AND mode=? AND run_id=? AND as_of=?",
                (instance_id, config_hash, mode, run_id, as_of),
            ).fetchone()
            if row is None:
                raise KeyError("decision observation is missing before execution planning")
            if str(row["plan_hash"]):
                if str(row["plan_hash"]) != execution_hashes["plan_hash"]:
                    raise ValueError("execution observation is immutable once attached")
                return dict(row)
            # The decision-time policy context and D+1 execution context are
            # both relevant. Preserve them as an ordered composite hash so
            # A comparison includes an execution plan only when both snapshots match,
            # without widening the public observation schema.
            combined = {
                key: hashlib.sha256(_json({
                    "decision_context": str(row[key]),
                    "execution_context": execution_hashes[key],
                }).encode("utf-8")).hexdigest()
                for key in ("account_hash", "quote_hash", "instrument_hash")
            }
            db.execute(
                "UPDATE decision_observations SET account_hash=?, quote_hash=?, "
                "instrument_hash=?, plan_hash=? WHERE observation_id=?",
                (
                    combined["account_hash"], combined["quote_hash"],
                    combined["instrument_hash"], execution_hashes["plan_hash"],
                    row["observation_id"],
                ),
            )
            updated = db.execute(
                "SELECT * FROM decision_observations WHERE observation_id=?",
                (row["observation_id"],),
            ).fetchone()
        assert updated is not None
        return dict(updated)

    def create_decision_comparison(
        self,
        instance_id: str,
        *,
        left_mode: str,
        left_run_id: str,
        right_mode: str,
        right_run_id: str,
        comparison_id: str | None = None,
    ) -> dict[str, Any]:
        current = self.get_instance(instance_id)
        identifier = str(comparison_id or uuid.uuid4().hex)
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO decision_comparisons "
                "(comparison_id,instance_id,config_hash,left_mode,left_run_id,right_mode,"
                "right_run_id,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,'running',?,?)",
                (
                    identifier, instance_id, current["config_hash"], str(left_mode),
                    str(left_run_id), str(right_mode), str(right_run_id), now, now,
                ),
            )
        return self.get_decision_comparison(identifier)

    def record_decision_comparison_result(
        self,
        comparison_id: str,
        session: str,
        *,
        status: str,
        reason: str = "",
        left_observation_id: str = "",
        right_observation_id: str = "",
        details: Any = None,
    ) -> None:
        if status not in {"match", "mismatch", "not_comparable"}:
            raise ValueError("invalid decision comparison result status")
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO decision_comparison_results VALUES (?,?,?,?,?,?,?,?)",
                (
                    comparison_id, str(session), status, str(reason),
                    left_observation_id, right_observation_id,
                    _json(details or {}), _now(),
                ),
            )

    def finish_decision_comparison(
        self,
        comparison_id: str,
        *,
        details: Any = None,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT status,COUNT(*) AS total FROM decision_comparison_results "
                "WHERE comparison_id=? GROUP BY status",
                (comparison_id,),
            ).fetchall()
            counts = {str(row["status"]): int(row["total"]) for row in rows}
            compared = sum(counts.values())
            terminal = (
                "completed"
                if compared and not counts.get("mismatch") and not counts.get("not_comparable")
                else "completed_with_differences"
            )
            cursor = db.execute(
                "UPDATE decision_comparisons SET status=?,compared_sessions=?,match_count=?,"
                "mismatch_count=?,not_comparable_count=?,details_json=?,updated_at=? "
                "WHERE comparison_id=? AND status='running'",
                (
                    terminal, compared, counts.get("match", 0), counts.get("mismatch", 0),
                    counts.get("not_comparable", 0), _json(details or {}), _now(),
                    comparison_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("decision comparison is missing or already terminal")
        return self.get_decision_comparison(comparison_id)

    def get_decision_comparison(self, comparison_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM decision_comparisons WHERE comparison_id=?", (comparison_id,)
            ).fetchone()
            results = db.execute(
                "SELECT * FROM decision_comparison_results WHERE comparison_id=? ORDER BY session",
                (comparison_id,),
            ).fetchall()
        if row is None:
            raise KeyError(f"unknown decision comparison {comparison_id!r}")
        return {
            **dict(row),
            "details": json.loads(row["details_json"] or "{}"),
            "results": [
                {**dict(item), "details": json.loads(item["details_json"] or "{}")}
                for item in results
            ],
        }

    def list_decision_comparisons(self, instance_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT comparison_id FROM decision_comparisons WHERE instance_id=? "
                "ORDER BY created_at,comparison_id",
                (instance_id,),
            ).fetchall()
        return [
            self.get_decision_comparison(str(row["comparison_id"])) for row in rows
        ]

    # ---- asynchronous backtest runs ------------------------------------ #

    def create_backtest_run(
        self,
        instance_id: str,
        request: dict[str, Any],
        *,
        run_id: str | None = None,
        origin: str = "trading",
        legacy_job_id: str = "",
    ) -> dict[str, Any]:
        current = self.get_instance(instance_id)
        identifier = str(run_id or uuid.uuid4().hex)
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO backtest_runs "
                "(run_id, instance_id, config_hash, status, request_json, created_at, updated_at, "
                "origin, legacy_job_id) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
                (
                    identifier, instance_id, current["config_hash"], _json(request), now, now,
                    str(origin), str(legacy_job_id),
                ),
            )
        return self.get_backtest_run(identifier)

    def update_backtest_run(
        self,
        run_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        artifact_dir: str = "",
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"queued", "running", "completed", "failed", "cancelled"}:
            raise ValueError("unsupported backtest run status")
        now = _now()
        started = now if status == "running" else ""
        ended = now if status in {"completed", "failed", "cancelled"} else ""
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE backtest_runs SET status=?, result_json=?, artifact_dir=?, error_json=?, "
                "started_at=CASE WHEN ?<>'' AND started_at='' THEN ? ELSE started_at END, "
                "ended_at=CASE WHEN ?<>'' THEN ? ELSE ended_at END, updated_at=? WHERE run_id=?",
                (
                    status, _json(result or {}), str(artifact_dir), _json(error or {}),
                    started, started, ended, ended, now, run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown backtest run {run_id!r}")
        return self.get_backtest_run(run_id)

    def request_backtest_cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE backtest_runs SET cancel_requested=1, updated_at=? "
                "WHERE run_id=? AND status IN ('queued','running')",
                (_now(), run_id),
            )
            if cursor.rowcount != 1:
                row = db.execute("SELECT status FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown backtest run {run_id!r}")
        return self.get_backtest_run(run_id)

    def get_backtest_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM backtest_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown backtest run {run_id!r}")
        return _backtest_run_row(row)

    def list_backtest_runs(self, instance_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if instance_id:
                rows = db.execute(
                    "SELECT * FROM backtest_runs WHERE instance_id=? ORDER BY created_at DESC",
                    (instance_id,),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM backtest_runs ORDER BY created_at DESC").fetchall()
        return [_backtest_run_row(row) for row in rows]

    def find_backtest_run_by_legacy_job(self, legacy_job_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM backtest_runs WHERE legacy_job_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (str(legacy_job_id),),
            ).fetchone()
        return None if row is None else _backtest_run_row(row)

    def import_legacy_backtest_job(
        self,
        legacy_job_id: str,
        *,
        instance_id: str,
        request: dict[str, Any],
        result: dict[str, Any],
        artifact_dir: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotently expose a completed 0.1.x job through formal run APIs."""

        legacy_id = str(legacy_job_id).strip()
        if not legacy_id:
            raise ValueError("legacy_job_id is required")
        current = self.get_instance(instance_id)
        existing = self.find_backtest_run_by_legacy_job(legacy_id)
        run_id = (
            existing["run_id"] if existing is not None
            else "legacy-import-" + hashlib.sha256(legacy_id.encode()).hexdigest()[:24]
        )
        now = _now()
        with self._lock, self._connect() as db:
            mapping = db.execute(
                "SELECT run_id FROM legacy_job_imports WHERE legacy_job_id=?",
                (legacy_id,),
            ).fetchone()
            if mapping is not None:
                return self.get_backtest_run(str(mapping["run_id"]))
            if existing is None:
                db.execute(
                    "INSERT INTO backtest_runs "
                    "(run_id, instance_id, config_hash, status, request_json, result_json, "
                    "artifact_dir, started_at, ended_at, created_at, updated_at, origin, "
                    "legacy_job_id) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, "
                    "'legacy_import', ?)",
                    (
                        run_id, instance_id, current["config_hash"], _json(request),
                        _json(result), str(artifact_dir), now, now, now, now, legacy_id,
                    ),
                )
            db.execute(
                "INSERT INTO legacy_job_imports "
                "(legacy_job_id, run_id, artifact_dir, imported_at, details_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (legacy_id, run_id, str(artifact_dir), now, _json(details or {})),
            )
        return self.get_backtest_run(run_id)

    def list_legacy_job_imports(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM legacy_job_imports ORDER BY imported_at, legacy_job_id"
            ).fetchall()
        return [
            {**dict(row), "details": json.loads(row["details_json"] or "{}")}
            for row in rows
        ]

    def legacy_runtime_blockers(self) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as db:
            instances = db.execute(
                "SELECT si.instance_id,si.validation_state,si.config_hash,si.updated_at "
                "FROM strategy_instances si LEFT JOIN deployment_runtime dr "
                "ON dr.instance_id=si.instance_id "
                "WHERE si.config_json LIKE '%\"legacy_daemon_import\":true%' "
                "AND COALESCE(dr.observed_state,'stopped') NOT IN ('stopped','error','ready') "
                "ORDER BY si.instance_id"
            ).fetchall()
            runs = db.execute(
                "SELECT run_id, instance_id, status, origin, legacy_job_id, updated_at "
                "FROM backtest_runs WHERE origin IN ('legacy_compatibility','legacy_import') "
                "AND status IN ('queued','running') ORDER BY created_at"
            ).fetchall()
        return {
            "instances": [dict(row) for row in instances],
            "backtest_runs": [dict(row) for row in runs],
        }

    def list_legacy_compatibility_runs(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT br.*, si.strategy_id, si.config_json FROM backtest_runs br "
                "JOIN strategy_instances si ON si.instance_id=br.instance_id "
                "WHERE br.origin='legacy_compatibility' ORDER BY br.created_at, br.run_id"
            ).fetchall()
        return [
            {
                **_backtest_run_row(row),
                "strategy_id": str(row["strategy_id"]),
                "instance_config": json.loads(row["config_json"] or "{}"),
            }
            for row in rows
        ]

    # ---- recoverable execution journal --------------------------------- #

    def save_execution_plan_state(
        self,
        plan_id: str,
        decision_id: str,
        instance_id: str,
        config_hash: str,
        *,
        phase: str,
        payload: Any,
        recovery_version: int = 1,
        next_child_index: dict[str, int] | None = None,
        last_error: Any = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO execution_plan_state "
                "(plan_id, decision_id, instance_id, config_hash, phase, recovery_version, "
                "next_child_index_json, payload_json, last_error_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(plan_id) DO UPDATE SET phase=excluded.phase, "
                "recovery_version=execution_plan_state.recovery_version+1, "
                "next_child_index_json=excluded.next_child_index_json, "
                "payload_json=excluded.payload_json, last_error_json=excluded.last_error_json, "
                "updated_at=excluded.updated_at",
                (
                    plan_id, decision_id, instance_id, config_hash, phase, int(recovery_version),
                    _json(next_child_index or {}), _json(payload), _json(last_error or {}), now, now,
                ),
            )
        return self.get_execution_plan_state(plan_id)

    def get_execution_plan_state(self, plan_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM execution_plan_state WHERE plan_id=?", (plan_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown execution plan state {plan_id!r}")
        return _execution_state_row(row)

    def list_unfinished_execution_plans(self, instance_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM execution_plan_state WHERE instance_id=? "
                "AND phase NOT IN ('completed','failed') ORDER BY created_at, plan_id",
                (instance_id,),
            ).fetchall()
        return [_execution_state_row(row) for row in rows]

    def latest_execution_target(self, instance_id: str, config_hash: str) -> dict[str, Any] | None:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload_json, updated_at FROM execution_plan_state WHERE instance_id=? "
                "AND config_hash=? AND phase='completed' ORDER BY updated_at DESC",
                (instance_id, config_hash),
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            if payload.get("route_mode") != "routed":
                continue
            target = payload.get("target")
            if isinstance(target, dict):
                return {**dict(target), "_execution_updated_at": row["updated_at"]}
        return None

    def record_plan_attempt(self, plan_id: str, phase: str, payload: Any = None) -> int:
        with self._lock, self._connect() as db:
            attempt = int(db.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 FROM plan_attempts WHERE plan_id=? AND phase=?",
                (plan_id, phase),
            ).fetchone()[0])
            db.execute(
                "INSERT INTO plan_attempts (plan_id, phase, attempt, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (plan_id, phase, attempt, _json(payload or {}), _now()),
            )
        return attempt

    def record_order_reconciliation(
        self,
        plan_id: str,
        reference: str,
        *,
        local_status: str,
        broker_status: str,
        payload: Any = None,
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO order_reconciliation "
                "(plan_id, reference, local_status, broker_status, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (plan_id, reference, local_status, broker_status, _json(payload or {}), _now()),
            )

    def record_fill_reconciliation(
        self,
        fill_key: str,
        plan_id: str,
        reference: str,
        *,
        order_id: str,
        volume: float,
        price: float,
        payload: Any = None,
    ) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO fill_reconciliation VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fill_key, plan_id, reference, order_id, float(volume), float(price),
                    _json(payload or {}), _now(),
                ),
            )
            return cursor.rowcount == 1

    # ---- operator authentication and audit ----------------------------- #

    def create_operator_token(
        self,
        token_id: str,
        operator_id: str,
        token_hash: str,
        *,
        label: str = "",
        expires_at: str = "",
    ) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO operator_tokens VALUES (?, ?, ?, ?, ?, ?, '', '')",
                (token_id, operator_id, token_hash, label, _now(), expires_at),
            )
        return self.get_operator_token(token_id)

    def get_operator_token(self, token_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM operator_tokens WHERE token_id=?", (token_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown operator token {token_id!r}")
        return _operator_token_row(row)

    def authenticate_operator_token(self, token_hash: str, *, now: str | None = None) -> dict[str, Any] | None:
        current = now or _now()
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM operator_tokens WHERE token_hash=? AND revoked_at='' "
                "AND (expires_at='' OR expires_at>?)",
                (token_hash, current),
            ).fetchone()
            if row is not None:
                db.execute(
                    "UPDATE operator_tokens SET last_used_at=? WHERE token_id=?",
                    (current, row["token_id"]),
                )
        return None if row is None else _operator_token_row(row)

    def revoke_operator_token(self, token_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE operator_tokens SET revoked_at=? WHERE token_id=? AND revoked_at=''",
                (_now(), token_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown or revoked operator token {token_id!r}")
        return self.get_operator_token(token_id)

    def record_audit_event(
        self,
        *,
        operator_id: str,
        request_id: str,
        action: str,
        reason: str,
        auth_source: str,
        result: str,
        instance_id: str = "",
        config_hash: str = "",
        account_id: str = "",
        broker: str = "",
        details: Any = None,
    ) -> int:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO operator_audit_events "
                "(operator_id, request_id, action, reason, auth_source, instance_id, config_hash, "
                "account_id, broker, result, details_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operator_id, request_id, action, reason, auth_source, instance_id,
                    config_hash, account_id, broker, result, _json(details or {}), _now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_audit_events(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM operator_audit_events ORDER BY event_id DESC LIMIT ?",
                (min(max(int(limit), 1), 2000),),
            ).fetchall()
        return [_audit_row(row) for row in rows]

    def register_compatibility_entrypoint(
        self,
        entrypoint: str,
        *,
        kind: str,
        replacement: str,
        deprecated_since: str,
        sunset_at: str,
        removal_release: str = "0.2.0",
        status: str = "deprecated",
        migration_cutoff: str = "",
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO compatibility_entrypoints VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entrypoint) DO UPDATE SET kind=excluded.kind, "
                "replacement=excluded.replacement, deprecated_since=excluded.deprecated_since, "
                "sunset_at=excluded.sunset_at, removal_release=excluded.removal_release, "
                "status=excluded.status, "
                "migration_cutoff=CASE WHEN excluded.migration_cutoff<>'' "
                "THEN excluded.migration_cutoff ELSE compatibility_entrypoints.migration_cutoff END, "
                "updated_at=excluded.updated_at",
                (
                    entrypoint, kind, replacement, deprecated_since, sunset_at,
                    removal_release, status, migration_cutoff, _now(),
                ),
            )

    def set_compatibility_cutoff(self, cutoff: str | None = None) -> str:
        value = str(cutoff or _now())
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE compatibility_entrypoints SET migration_cutoff=?, updated_at=?",
                (value, _now()),
            )
            if self._environment_id:
                db.execute(
                    "UPDATE compatibility_environments SET migration_cutoff=?, "
                    "last_reported_at=? WHERE environment_id=?",
                    (value, _now(), self._environment_id),
                )
        return value

    def register_compatibility_environment(self, environment_id: str) -> dict[str, Any]:
        value = str(environment_id).strip()
        if not value:
            raise ValueError("compatibility environment_id is required")
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO compatibility_environments "
                "(environment_id, registered_at, last_reported_at, source) "
                "VALUES (?, ?, ?, 'local') "
                "ON CONFLICT(environment_id) DO UPDATE SET "
                "last_reported_at=excluded.last_reported_at, source='local'",
                (value, now, now),
            )
        self._environment_id = value
        return self.compatibility_environment_status(value)

    def compatibility_environment_status(
        self,
        environment_id: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        clauses = "" if environment_id is None else "WHERE environment_id=?"
        values: tuple[Any, ...] = () if environment_id is None else (str(environment_id),)
        with self._connect() as db:
            rows = db.execute(
                "SELECT ce.*, CASE WHEN ce.source='imported' "
                "THEN ce.reported_post_cutoff_count ELSE "
                "(SELECT COUNT(*) FROM legacy_usage_events le "
                " WHERE le.environment_id=ce.environment_id "
                " AND ce.migration_cutoff<>'' AND le.used_at>=ce.migration_cutoff) END "
                "AS post_cutoff_count "
                "FROM compatibility_environments ce " + clauses + " ORDER BY environment_id",
                values,
            ).fetchall()
        result = [
            {**dict(row), "evidence": json.loads(row["evidence_json"] or "{}")}
            for row in rows
        ]
        if environment_id is None:
            return result
        if not result:
            raise KeyError(f"unknown compatibility environment {environment_id!r}")
        return result[0]

    def compatibility_environment_entrypoints(
        self,
        environment_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT ce.entrypoint, ce.kind, ce.replacement, ce.sunset_at, "
                "env.migration_cutoff, (SELECT COUNT(*) FROM legacy_usage_events le "
                "WHERE le.environment_id=env.environment_id AND le.entrypoint=ce.entrypoint "
                "AND env.migration_cutoff<>'' AND le.used_at>=env.migration_cutoff) "
                "AS post_cutoff_count FROM compatibility_entrypoints ce "
                "JOIN compatibility_environments env ON env.environment_id=? "
                "ORDER BY ce.entrypoint",
                (str(environment_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_compatibility_environment_report(
        self,
        payload: dict[str, Any],
        *,
        evidence_hash: str,
        imported: bool,
    ) -> dict[str, Any]:
        environment_id = str(payload.get("environment_id") or "").strip()
        cutoff = str(payload.get("migration_cutoff") or "").strip()
        generated_at = str(payload.get("generated_at") or _now())
        if not environment_id or not cutoff or not str(evidence_hash).strip():
            raise ValueError("compatibility environment report binding is incomplete")
        post_cutoff = max(int(payload.get("post_cutoff_count") or 0), 0)
        source = "imported" if imported else "local"
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO compatibility_environments "
                "(environment_id, registered_at, last_reported_at, migration_cutoff, source, "
                "reported_post_cutoff_count, evidence_hash, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(environment_id) DO UPDATE SET "
                "last_reported_at=excluded.last_reported_at, "
                "migration_cutoff=excluded.migration_cutoff, source=excluded.source, "
                "reported_post_cutoff_count=excluded.reported_post_cutoff_count, "
                "evidence_hash=excluded.evidence_hash, evidence_json=excluded.evidence_json",
                (
                    environment_id, generated_at, generated_at, cutoff, source,
                    post_cutoff, str(evidence_hash), _json(payload),
                ),
            )
        return self.compatibility_environment_status(environment_id)

    def record_legacy_usage(
        self,
        entrypoint: str,
        details: Any = None,
        *,
        client_kind: str = "",
        client_version: str = "",
        source: str = "",
        request_id: str = "",
        environment_id: str = "",
    ) -> None:
        request_hash = (
            hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()[:24]
            if request_id else ""
        )
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO legacy_usage VALUES (?, 1, ?, ?) "
                "ON CONFLICT(entrypoint) DO UPDATE SET call_count=call_count+1, "
                "last_used_at=excluded.last_used_at, details_json=excluded.details_json",
                (entrypoint, _now(), _json(details or {})),
            )
            db.execute(
                "INSERT INTO legacy_usage_events "
                "(entrypoint, client_kind, client_version, source, request_id_hash, "
                "environment_id, used_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entrypoint, str(client_kind), str(client_version), str(source),
                    request_hash, str(environment_id or self._environment_id), _now(),
                ),
            )

    def list_legacy_usage(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM legacy_usage ORDER BY entrypoint").fetchall()
        return [
            {**dict(row), "details": json.loads(row["details_json"] or "{}")}
            for row in rows
        ]

    def compatibility_status(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT ce.*, COALESCE(lu.call_count, 0) AS call_count, "
                "COALESCE(lu.last_used_at, '') AS last_used_at, "
                "(SELECT COUNT(*) FROM legacy_usage_events le WHERE le.entrypoint=ce.entrypoint "
                "AND ce.migration_cutoff<>'' AND le.used_at>=ce.migration_cutoff) AS post_cutoff_count "
                "FROM compatibility_entrypoints ce LEFT JOIN legacy_usage lu "
                "ON lu.entrypoint=ce.entrypoint ORDER BY ce.entrypoint"
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- Broker UAT evidence ------------------------------------------ #

    def create_broker_uat_run(
        self,
        *,
        broker: str,
        account_hash: str,
        environment: str,
        plugin_version: str,
        plugin_hash: str,
        sdk_version: str,
        symbol: str,
        max_notional: float,
        scenario_version: int = 1,
        code_commit: str = "",
        runtime_code_hash: str = "",
        sdk_hash: str = "",
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if str(broker).lower() not in {"xtp", "emt", "tts"}:
            raise ValueError("broker UAT supports xtp, emt or tts")
        if float(max_notional) <= 0:
            raise ValueError("broker UAT max_notional must be positive")
        if int(scenario_version) not in {1, 2}:
            raise ValueError("unsupported broker UAT scenario version")
        identifier = str(run_id or uuid.uuid4().hex)
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO broker_uat_runs "
                "(run_id, broker, account_hash, environment, plugin_version, plugin_hash, "
                "sdk_version, status, symbol, max_notional, scenario_version, code_commit, "
                "runtime_code_hash, sdk_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier, str(broker).lower(), account_hash, environment,
                    plugin_version, plugin_hash, sdk_version, symbol,
                    float(max_notional), int(scenario_version), str(code_commit),
                    str(runtime_code_hash), str(sdk_hash), now, now,
                ),
            )
        return self.get_broker_uat_run(identifier)

    def update_broker_uat_step(
        self,
        run_id: str,
        step: str,
        *,
        status: str,
        evidence: Any = None,
        error: Any = None,
    ) -> dict[str, Any]:
        if status not in {"running", "passed", "failed", "aborted"}:
            raise ValueError("invalid broker UAT step status")
        now = _now()
        ended_at = now if status in {"passed", "failed", "aborted"} else ""
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO broker_uat_steps "
                "(run_id, step, status, evidence_json, error_json, started_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, step) DO UPDATE SET status=excluded.status, "
                "evidence_json=excluded.evidence_json, error_json=excluded.error_json, "
                "ended_at=excluded.ended_at",
                (
                    run_id, step, status, _json(evidence or {}), _json(error or {}),
                    now, ended_at,
                ),
            )
            run_status = "failed" if status == "failed" else "running"
            if status == "aborted":
                run_status = "aborted"
            db.execute(
                "UPDATE broker_uat_runs SET status=?, current_step=?, error_json=?, "
                "updated_at=?, ended_at=? WHERE run_id=?",
                (
                    run_status, step, _json(error or {}), now,
                    ended_at if status in {"failed", "aborted"} else "", run_id,
                ),
            )
        return self.get_broker_uat_run(run_id)

    def bind_broker_uat_account(self, run_id: str, account_hash: str) -> dict[str, Any]:
        value = str(account_hash).strip()
        if not value:
            raise ValueError("broker UAT account_hash is required")
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE broker_uat_runs SET account_hash=?, updated_at=? "
                "WHERE run_id=? AND account_hash='' AND status IN ('created','running')",
                (value, _now(), run_id),
            )
            if cursor.rowcount != 1:
                row = db.execute(
                    "SELECT account_hash FROM broker_uat_runs WHERE run_id=?", (run_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"unknown broker UAT run {run_id!r}")
                if str(row["account_hash"]) != value:
                    raise ValueError("broker UAT account binding is immutable")
        return self.get_broker_uat_run(run_id)

    def claim_broker_uat_route(
        self,
        run_id: str,
        reference: str,
        notional: float = 0.0,
    ) -> bool:
        """Atomically reserve one bounded real-order reference for a UAT run.

        The claim is deliberately never released. If a process dies after the
        broker accepts an order but before its callback is journalled, recovery
        must reconcile that unknown outcome instead of submitting again.
        """

        identifier = str(run_id).strip()
        stable_reference = str(reference).strip()
        if not identifier or not stable_reference:
            return False
        amount = max(float(notional), 0.0)
        with self._lock, self._connect() as db:
            run = db.execute(
                "SELECT status, max_notional FROM broker_uat_runs WHERE run_id=?",
                (identifier,),
            ).fetchone()
            if run is None or str(run["status"]) not in {"created", "running"}:
                return False
            if db.execute(
                "SELECT 1 FROM broker_uat_route_claims WHERE run_id=? AND reference=?",
                (identifier, stable_reference),
            ).fetchone() is not None:
                return False
            claimed = float(db.execute(
                "SELECT COALESCE(SUM(notional), 0) FROM broker_uat_route_claims "
                "WHERE run_id=?",
                (identifier,),
            ).fetchone()[0])
            if claimed + amount > float(run["max_notional"]) + 1e-9:
                return False
            cursor = db.execute(
                "INSERT OR IGNORE INTO broker_uat_route_claims "
                "(run_id, reference, claimed_at, notional) "
                "SELECT run_id, ?, ?, ? FROM broker_uat_runs "
                "WHERE run_id=? AND status IN ('created','running')",
                (stable_reference, _now(), amount, identifier),
            )
            if cursor.rowcount == 1:
                db.execute(
                    "UPDATE broker_uat_runs SET requested_notional=?, updated_at=? "
                    "WHERE run_id=?",
                    (claimed + amount, _now(), identifier),
                )
            return cursor.rowcount == 1

    def update_broker_uat_filled_notional(
        self,
        run_id: str,
        filled_notional: float,
    ) -> dict[str, Any]:
        amount = max(float(filled_notional), 0.0)
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE broker_uat_runs SET filled_notional=?, updated_at=? WHERE run_id=?",
                (amount, _now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown broker UAT run {run_id!r}")
        return self.get_broker_uat_run(run_id)

    def record_broker_uat_order_event(
        self,
        run_id: str,
        *,
        reference: str,
        order_id: str,
        status: str,
        traded: float,
        volume: float,
        payload: Any,
        observed_at: str = "",
    ) -> bool:
        safe_payload = payload if isinstance(payload, dict) else {"value": payload}
        material = {
            "run_id": str(run_id),
            "reference": str(reference),
            "order_id": str(order_id),
            "status": str(status),
            "traded": float(traded),
            "volume": float(volume),
            "payload": safe_payload,
            "observed_at": str(observed_at),
        }
        event_hash = hashlib.sha256(_json(material).encode("utf-8")).hexdigest()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO broker_uat_order_events "
                "(event_hash, run_id, reference, order_id, status, traded, volume, "
                "payload_json, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_hash, str(run_id), str(reference), str(order_id), str(status),
                    float(traded), float(volume), _json(safe_payload),
                    str(observed_at or _now()),
                ),
            )
            return cursor.rowcount == 1

    def abort_broker_uat_run(self, run_id: str, *, reason: str) -> dict[str, Any]:
        if not str(reason).strip():
            raise ValueError("broker UAT abort requires a reason")
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE broker_uat_runs SET status='aborted', error_json=?, "
                "updated_at=?, ended_at=? WHERE run_id=? "
                "AND status IN ('created','running','failed','restart_required')",
                (_json({"reason": str(reason)}), _now(), _now(), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("broker UAT run is missing or already terminal")
        return self.get_broker_uat_run(run_id)

    def resume_broker_uat_run(self, run_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE broker_uat_runs SET status='running', error_json='{}', "
                "updated_at=?, ended_at='' WHERE run_id=? "
                "AND status IN ('created','failed','restart_required')",
                (_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("broker UAT run is not resumable")
        return self.get_broker_uat_run(run_id)

    def mark_broker_uat_restart_required(
        self,
        run_id: str,
        *,
        process_marker: str,
    ) -> dict[str, Any]:
        marker = str(process_marker).strip()
        if not marker:
            raise ValueError("broker UAT process marker is required")
        self.update_broker_uat_step(
            run_id,
            "process_restart_required",
            status="passed",
            evidence={"origin_process_marker": marker},
        )
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE broker_uat_runs SET status='restart_required', "
                "current_step='process_restart_required', error_json='{}', "
                "updated_at=?, ended_at='' WHERE run_id=? AND status='running'",
                (_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("broker UAT run cannot enter the restart checkpoint")
        return self.get_broker_uat_run(run_id)

    def complete_broker_uat_run(
        self,
        run_id: str,
        *,
        capabilities: list[str],
        expires_at: str,
    ) -> dict[str, Any]:
        run = self.get_broker_uat_run(run_id)
        if int(run.get("scenario_version") or 1) >= 2:
            required_steps = {
                "preflight", "connected", "execution_plan",
                "marketable_order_acknowledged", "marketable_fill_observed",
                "remainder_order_acknowledged", "plan_partial_execution_observed",
                "process_restart_required", "restart_reconciled",
                "kill_switches_verified", "cancel_confirmed", "reconnect_reconciled",
            }
        else:
            required_steps = {
                "preflight", "connected", "order_acknowledged", "partial_fill_observed",
                "process_restart_required", "restart_reconciled",
                "kill_switches_verified", "cancel_confirmed", "reconnect_reconciled",
            }
        statuses = {str(step["step"]): str(step["status"]) for step in run["steps"]}
        missing = sorted(required_steps - set(statuses))
        failed = sorted(step for step in required_steps if statuses.get(step) != "passed")
        if missing or failed:
            raise ValueError(
                "all required broker UAT steps must pass before evidence is issued: "
                f"missing={missing}, not_passed={failed}"
            )
        if not str(run["account_hash"]) or not str(run["plugin_hash"]):
            raise ValueError("broker UAT account and plugin bindings are required")
        if int(run.get("scenario_version") or 1) >= 2 and not all(
            str(run.get(field) or "")
            for field in ("code_commit", "runtime_code_hash", "sdk_hash")
        ):
            raise ValueError(
                "Broker UAT v2 evidence requires code, runtime and native SDK fingerprints"
            )
        if str(expires_at) <= _now():
            raise ValueError("broker UAT evidence expiry must be in the future")
        payload = {
            "run_id": run_id,
            "broker": run["broker"],
            "account_hash": run["account_hash"],
            "environment": run["environment"],
            "plugin_version": run["plugin_version"],
            "plugin_hash": run["plugin_hash"],
            "sdk_version": run["sdk_version"],
            "sdk_hash": run["sdk_hash"],
            "scenario_version": int(run["scenario_version"]),
            "code_commit": run["code_commit"],
            "runtime_code_hash": run["runtime_code_hash"],
            "requested_notional": float(run["requested_notional"]),
            "filled_notional": float(run["filled_notional"]),
            "capabilities": sorted(set(capabilities)),
            "expires_at": str(expires_at),
        }
        evidence_hash = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
        evidence_id = uuid.uuid4().hex
        now = _now()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO broker_uat_evidence "
                "(evidence_id, run_id, broker, account_hash, environment, plugin_version, "
                "plugin_hash, sdk_version, capabilities_json, evidence_hash, passed_at, expires_at, "
                "scenario_version, code_commit, runtime_code_hash, sdk_hash, requested_notional, "
                "filled_notional) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id, run_id, run["broker"], run["account_hash"],
                    run["environment"], run["plugin_version"], run["plugin_hash"],
                    run["sdk_version"],
                    _json(payload["capabilities"]), evidence_hash, now, str(expires_at),
                    int(run["scenario_version"]), run["code_commit"],
                    run["runtime_code_hash"], run["sdk_hash"],
                    float(run["requested_notional"]), float(run["filled_notional"]),
                ),
            )
            db.execute(
                "UPDATE broker_uat_runs SET status='passed', current_step='completed', "
                "updated_at=?, ended_at=? WHERE run_id=?",
                (now, now, run_id),
            )
        return self.get_broker_uat_run(run_id)

    def get_broker_uat_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM broker_uat_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            steps = db.execute(
                "SELECT * FROM broker_uat_steps WHERE run_id=? ORDER BY started_at, step",
                (run_id,),
            ).fetchall()
            evidence = db.execute(
                "SELECT * FROM broker_uat_evidence WHERE run_id=?", (run_id,)
            ).fetchone()
            order_events = db.execute(
                "SELECT * FROM broker_uat_order_events WHERE run_id=? "
                "ORDER BY observed_at, rowid",
                (run_id,),
            ).fetchall()
        if row is None:
            raise KeyError(f"unknown broker UAT run {run_id!r}")
        return {
            **dict(row),
            "error": json.loads(row["error_json"] or "{}"),
            "steps": [
                {
                    **dict(item),
                    "evidence": json.loads(item["evidence_json"] or "{}"),
                    "error": json.loads(item["error_json"] or "{}"),
                }
                for item in steps
            ],
            "order_events": [
                {**dict(item), "payload": json.loads(item["payload_json"] or "{}")}
                for item in order_events
            ],
            "evidence": None if evidence is None else {
                **dict(evidence),
                "capabilities": json.loads(evidence["capabilities_json"] or "[]"),
            },
        }

    def list_broker_uat_runs(self, broker: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if broker:
                rows = db.execute(
                    "SELECT run_id FROM broker_uat_runs WHERE broker=? ORDER BY created_at DESC",
                    (str(broker).lower(),),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT run_id FROM broker_uat_runs ORDER BY created_at DESC"
                ).fetchall()
        return [self.get_broker_uat_run(str(row["run_id"])) for row in rows]

    def valid_broker_uat_evidence(
        self,
        broker: str,
        *,
        account_hash: str = "",
        environment: str = "",
        plugin_version: str = "",
        plugin_hash: str = "",
        sdk_version: str = "",
        sdk_hash: str = "",
        runtime_code_hash: str = "",
        scenario_version: int = 0,
        passed_after: str = "",
        now: str | None = None,
    ) -> dict[str, Any] | None:
        current = str(now or _now())
        clauses = ["broker=?", "expires_at>?"]
        values: list[Any] = [str(broker).lower(), current]
        if account_hash:
            clauses.append("account_hash=?")
            values.append(account_hash)
        if environment:
            clauses.append("environment=?")
            values.append(environment)
        if plugin_version:
            clauses.append("plugin_version=?")
            values.append(plugin_version)
        if plugin_hash:
            clauses.append("plugin_hash=?")
            values.append(plugin_hash)
        if sdk_version:
            clauses.append("sdk_version=?")
            values.append(sdk_version)
        if sdk_hash:
            clauses.append("sdk_hash=?")
            values.append(sdk_hash)
        if runtime_code_hash:
            clauses.append("runtime_code_hash=?")
            values.append(runtime_code_hash)
        if scenario_version:
            clauses.append("scenario_version=?")
            values.append(int(scenario_version))
        if passed_after:
            clauses.append("passed_at>=?")
            values.append(str(passed_after))
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM broker_uat_evidence WHERE " + " AND ".join(clauses)
                + " ORDER BY passed_at DESC LIMIT 1",
                values,
            ).fetchone()
        if row is None:
            return None
        return {**dict(row), "capabilities": json.loads(row["capabilities_json"] or "[]")}


def _instance_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["config_json"])
    if not isinstance(payload, dict):
        raise RuntimeError("strategy instance config_json is not an object")
    if str(payload.get("config_hash") or "") != str(row["config_hash"]):
        raise RuntimeError("strategy instance config JSON/hash projection is inconsistent")
    try:
        config = StrategyInstanceConfig.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"strategy instance config is corrupted: {exc}") from exc
    if (
        config.strategy_id != str(row["strategy_id"])
        or config.strategy_version != str(row["strategy_version"])
    ):
        raise RuntimeError("strategy instance row/config projection is inconsistent")
    return {
        "instance_id": row["instance_id"],
        "strategy_id": row["strategy_id"],
        "strategy_version": row["strategy_version"],
        "config": config.to_dict(),
        "config_hash": row["config_hash"],
        "validation_state": row["validation_state"],
        "updated_at": row["updated_at"],
    }


def _rehash_instances_for_v4(db: sqlite3.Connection) -> None:
    """Bind pre-v4 instances to the artifact-aware hash and invalidate evidence."""

    rows = db.execute("SELECT * FROM strategy_instances").fetchall()
    now = _now()
    for row in rows:
        payload = json.loads(row["config_json"] or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid config JSON for strategy instance {row['instance_id']}")
        payload.update({
            "instance_id": str(row["instance_id"]),
            "strategy_id": str(row["strategy_id"]),
            "strategy_version": str(row["strategy_version"]),
            "deployment_level": "replay",
            "config_hash": "",
        })
        payload.setdefault("artifact_binding", {})
        config = StrategyInstanceConfig.from_dict(payload)
        db.execute(
            "UPDATE strategy_instances SET config_json=?, config_hash=?, lifecycle=?, "
            "deployment_level=?, updated_at=? WHERE instance_id=?",
            (
                _json(config.to_dict()), config.config_hash, LifecycleState.VALIDATED.value,
                "replay", now, config.instance_id,
            ),
        )
        db.execute("DELETE FROM stage_evidence WHERE instance_id=?", (config.instance_id,))
        db.execute(
            "UPDATE stage_runs SET status='invalidated', ended_at=?, updated_at=? "
            "WHERE instance_id=? AND status='running'",
            (now, now, config.instance_id),
        )
        db.execute(
            "UPDATE deployment_runtime SET config_hash=?, deployment_level=?, account_id='', "
            "broker='', desired_state=?, observed_state=?, runtime_id='', runner_heartbeat_at='', "
            "last_error_json=?, reconcile_required=0, binding_active=0, version=version+1, "
            "updated_at=? WHERE instance_id=?",
            (
                config.config_hash, "replay",
                LifecycleState.VALIDATED.value, LifecycleState.VALIDATED.value,
                _json({"rule": "config_hash_schema_upgrade", "reason": "v4 artifact binding"}),
                now, config.instance_id,
            ),
        )


def _rehash_instances_for_v6(db: sqlite3.Connection) -> None:
    """Bind exact evaluation history and policy ownership into config hashes."""

    rows = db.execute("SELECT * FROM strategy_instances").fetchall()
    now = _now()
    for row in rows:
        payload = json.loads(row["config_json"] or "{}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid config JSON for strategy instance {row['instance_id']}")
        old_hash = str(row["config_hash"])
        params = dict(payload.get("params") or {})
        data_policy = dict(payload.get("data_policy") or {})
        windows = {
            str(key): int(value)
            for key, value in params.items()
            if "window" in str(key)
            and isinstance(value, int)
            and not isinstance(value, bool)
            and int(value) > 0
        }
        if "rsi_window" in windows and "stoch_window" in windows:
            inferred = windows["rsi_window"] + windows["stoch_window"] + 1
        else:
            inferred = max(windows.values(), default=0) + 1
        data_policy["history_window"] = max(
            int(data_policy.get("history_window") or 0), inferred, 1,
        )
        policy = dict(payload.get("portfolio_policy") or {})
        if str(policy.get("policy_id") or "") == "timing_fixed_exposure":
            policy_params = dict(policy.get("params") or {})
            legacy_target = params.pop("target_percent", None)
            if legacy_target is not None:
                policy_params.setdefault("target_percent", float(legacy_target))
            policy["params"] = policy_params
        payload.update({
            "instance_id": str(row["instance_id"]),
            "strategy_id": str(row["strategy_id"]),
            "strategy_version": str(row["strategy_version"]),
            "params": params,
            "data_policy": data_policy,
            "portfolio_policy": policy,
            "deployment_level": "replay",
            "config_hash": "",
        })
        config = StrategyInstanceConfig.from_dict(payload)
        db.execute(
            "UPDATE strategy_instances SET config_json=?, config_hash=?, lifecycle=?, "
            "deployment_level=?, updated_at=? WHERE instance_id=?",
            (
                _json(config.to_dict()), config.config_hash, LifecycleState.VALIDATED.value,
                "replay", now, config.instance_id,
            ),
        )
        db.execute("DELETE FROM stage_evidence WHERE instance_id=?", (config.instance_id,))
        db.execute("DELETE FROM live_approvals WHERE instance_id=?", (config.instance_id,))
        db.execute("DELETE FROM account_baselines WHERE instance_id=?", (config.instance_id,))
        db.execute("DELETE FROM provider_checkpoints WHERE instance_id=?", (config.instance_id,))
        db.execute(
            "UPDATE stage_runs SET status='invalidated', ended_at=?, updated_at=? "
            "WHERE instance_id=? AND status='running'",
            (now, now, config.instance_id),
        )
        db.execute(
            "UPDATE artifact_manifests SET config_hash=? "
            "WHERE instance_id=? AND config_hash=?",
            (config.config_hash, config.instance_id, old_hash),
        )
        db.execute(
            "UPDATE deployment_runtime SET config_hash=?, deployment_level=?, account_id='', "
            "broker='', desired_state=?, observed_state=?, runtime_id='', runner_heartbeat_at='', "
            "last_command_id='', last_error_json=?, reconcile_required=0, binding_active=0, "
            "version=version+1, updated_at=? WHERE instance_id=?",
            (
                config.config_hash, "replay",
                LifecycleState.VALIDATED.value, LifecycleState.VALIDATED.value,
                _json({
                    "rule": "deterministic_history_schema_upgrade",
                    "reason": "v6 exact history window and portfolio policy ownership",
                }),
                now, config.instance_id,
            ),
        )


def _runtime_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "instance_id": row["instance_id"],
        "config_hash": row["config_hash"],
        "run_mode": row["run_mode"],
        "account_id": row["account_id"],
        "execution_environment": row["execution_environment"],
        "trade_provider": row["trade_provider"],
        "quote_provider": row["quote_provider"],
        "account_profile": row["account_profile"],
        "quote_data_kind": row["quote_data_kind"],
        "binding_hash": row["binding_hash"],
        "desired_state": row["desired_state"],
        "observed_state": row["observed_state"],
        "runtime_id": row["runtime_id"],
        "runner_heartbeat_at": row["runner_heartbeat_at"],
        "last_command_id": row["last_command_id"],
        "last_error": json.loads(row["last_error_json"] or "{}"),
        "reconcile_required": bool(row["reconcile_required"]),
        "reconciled": bool(row["reconciled"]),
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


def _runtime_run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "instance_id": row["instance_id"],
        "run_mode": row["run_mode"],
        "config_hash": row["config_hash"],
        "binding_hash": row["binding_hash"],
        "status": row["status"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "trading_sessions": int(row["trading_sessions"]),
        "metrics": json.loads(row["metrics_json"] or "{}"),
        "updated_at": row["updated_at"],
    }


def _deployment_spec_row(row: sqlite3.Row) -> dict[str, Any]:
    try:
        spec = DeploymentSpec(
            instance_id=str(row["instance_id"]),
            config_hash=str(row["config_hash"]),
            run_mode=str(row["run_mode"]),
            execution_environment=str(row["execution_environment"]),
            trade_provider=str(row["trade_provider"]),
            quote_provider=str(row["quote_provider"]),
            account_profile=str(row["account_profile"]),
            account_id=str(row["account_id"]),
            quote_data_kind=str(row["quote_data_kind"]),
            binding_hash=str(row["binding_hash"]),
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"deployment configuration is corrupted: {exc}") from exc
    return {
        **spec.to_dict(),
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


def _artifact_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "artifact_id": row["artifact_id"],
        "instance_id": row["instance_id"],
        "config_hash": row["config_hash"],
        "artifact_type": row["artifact_type"],
        "manifest": json.loads(row["manifest_json"] or "{}"),
        "created_at": row["created_at"],
    }


def _portfolio_decision_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "decision_id": row["decision_id"],
        "instance_id": row["instance_id"],
        "config_hash": row["config_hash"],
        "as_of": row["as_of"],
        "effective_session": row["effective_session"],
        "valid_until": row["valid_until"],
        "status": row["status"],
        "decision": json.loads(row["payload_json"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _backtest_run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "instance_id": row["instance_id"],
        "config_hash": row["config_hash"],
        "status": row["status"],
        "request": json.loads(row["request_json"] or "{}"),
        "result": json.loads(row["result_json"] or "{}"),
        "artifact_dir": row["artifact_dir"],
        "error": json.loads(row["error_json"] or "{}"),
        "cancel_requested": bool(row["cancel_requested"]),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "origin": row["origin"],
        "legacy_job_id": row["legacy_job_id"],
    }


def _live_execution_quality_metrics(
    db: sqlite3.Connection,
    run: sqlite3.Row,
) -> dict[str, Any]:
    """Derive implementation shortfall from reconciled Broker callbacks."""

    rows = db.execute(
        "SELECT fr.fill_key, fr.reference, fr.order_id, fr.volume, fr.price, "
        "co.payload_json FROM fill_reconciliation fr "
        "JOIN execution_plan_state eps ON eps.plan_id=fr.plan_id "
        "JOIN child_orders co ON co.reference=fr.reference AND co.plan_id=fr.plan_id "
        "WHERE eps.instance_id=? AND eps.config_hash=? AND fr.created_at>=? "
        "ORDER BY fr.fill_key",
        (run["instance_id"], run["config_hash"], run["started_at"]),
    ).fetchall()
    journal: list[dict[str, Any]] = []
    for row in rows:
        child = json.loads(row["payload_json"] or "{}")
        journal.append(
            {
                "fill_key": str(row["fill_key"]),
                "order_reference": str(row["reference"]),
                "order_id": str(row["order_id"]),
                "side": str(child.get("side") or ""),
                "arrival_price": child.get("price"),
                "fill_price": row["price"],
                "volume": row["volume"],
            }
        )
    fingerprint = hashlib.sha256(
        _json(journal).encode("utf-8")
    ).hexdigest()
    if not journal:
        return {
            "execution_quality_source": "broker_fill_reconciliation",
            "execution_quality_order_count": 0,
            "median_implementation_shortfall_bp": None,
            "p95_implementation_shortfall_bp": None,
            "execution_quality_passed": False,
            "execution_quality_failures": ["missing_broker_fills"],
            "execution_quality_fingerprint": fingerprint,
        }

    import pandas as pd

    from alphapilot.systems.research.execution_quality import (
        evaluate_implementation_shortfall,
    )

    quality = evaluate_implementation_shortfall(pd.DataFrame(journal))
    return {
        "execution_quality_source": "broker_fill_reconciliation",
        "execution_quality_order_count": quality["order_count"],
        "median_implementation_shortfall_bp": quality["median_bp"],
        "p95_implementation_shortfall_bp": quality["p95_bp"],
        "execution_quality_passed": quality["passed"],
        "execution_quality_failures": quality["failures"],
        "execution_quality_fingerprint": fingerprint,
    }


def _execution_state_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "plan_id": row["plan_id"],
        "decision_id": row["decision_id"],
        "instance_id": row["instance_id"],
        "config_hash": row["config_hash"],
        "phase": row["phase"],
        "recovery_version": int(row["recovery_version"]),
        "next_child_index": json.loads(row["next_child_index_json"] or "{}"),
        "payload": json.loads(row["payload_json"] or "{}"),
        "last_error": json.loads(row["last_error_json"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _operator_token_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "token_id": row["token_id"],
        "operator_id": row["operator_id"],
        "label": row["label"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "last_used_at": row["last_used_at"],
    }


def _audit_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **dict(row),
        "details": json.loads(row["details_json"] or "{}"),
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


def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


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


def _binding_hash(
    instance_id: str,
    execution_environment: str,
    trade_provider: str,
    quote_provider: str,
    account_profile: str,
    quote_data_kind: str,
) -> str:
    raw = _json({
        "instance_id": str(instance_id),
        "execution_environment": str(execution_environment).strip().lower(),
        "trade_provider": str(trade_provider).strip().lower(),
        "quote_provider": str(quote_provider).strip().lower(),
        "account_profile": str(account_profile).strip(),
        "quote_data_kind": str(quote_data_kind).strip().lower(),
    })
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
