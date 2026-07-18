"""Deterministic daily replay built on the same decision and planning core."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from alphapilot.systems.trading.application import (
    DecisionPipeline,
    WarmupRequired,
    canonical_hash,
)
from alphapilot.systems.trading.contracts import (
    AccountSnapshot,
    CompletedBar,
    ExecutionChild,
    ExecutionPhase,
    ExecutionPlan,
    FeeSchedule,
    InstrumentMetadata,
    PlanIssue,
    PortfolioDecision,
    PriceAdjustment,
    TradableQuote,
    canonical_instrument,
)
from alphapilot.systems.trading.data_adapters import SequenceCalendar
from alphapilot.systems.trading.domain import StrategyInstanceConfig
from alphapilot.systems.trading.execution import ExecutionCoordinator
from alphapilot.systems.trading.planning import ExecutionPlanner
from alphapilot.systems.trading.store import StrategyRuntimeStore


@dataclass(frozen=True)
class ReplayConfig:
    initial_cash: float = 100_000.0
    open_cost: float = 0.00015
    close_cost: float = 0.00015
    min_cost: float = 5.0
    slippage: float = 0.0
    lot_size: int = 100
    max_order_value: float = 0.0
    partial_fill_ratio: float = 1.0
    instrument_metadata: Mapping[str, InstrumentMetadata] = field(default_factory=dict)
    quote_overrides: Mapping[str, Mapping[str, TradableQuote]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if float(self.initial_cash) <= 0:
            raise ValueError("replay initial_cash must be positive")
        if min(float(self.open_cost), float(self.close_cost), float(self.min_cost)) < 0:
            raise ValueError("replay costs must not be negative")
        if not 0 <= float(self.slippage) < 1:
            raise ValueError("replay slippage must be in [0, 1)")
        if int(self.lot_size) <= 0:
            raise ValueError("replay lot_size must be positive")
        if float(self.max_order_value) < 0:
            raise ValueError("replay max_order_value must not be negative")
        if not 0 <= float(self.partial_fill_ratio) <= 1:
            raise ValueError("replay partial_fill_ratio must be in [0, 1]")


@dataclass(frozen=True)
class ReplayResult:
    run_id: str
    summary: dict[str, Any]
    artifact_dir: Path
    manifest: dict[str, Any]


@dataclass
class _ReplayAccount:
    cash: float
    positions: dict[str, float] = field(default_factory=dict)
    acquired_session: dict[str, str] = field(default_factory=dict)
    settlement_days: dict[str, int] = field(default_factory=dict)

    def snapshot(
        self,
        session: str,
        prices: Mapping[str, float],
        *,
        account_id: str = "replay",
    ) -> AccountSnapshot:
        equity = self.cash + sum(
            float(volume) * float(prices.get(instrument, 0.0))
            for instrument, volume in self.positions.items()
        )
        sellable = {
            instrument: (
                float(volume)
                if (
                    int(self.settlement_days.get(instrument, 1)) <= 0
                    or self.acquired_session.get(instrument, "")[:10] < session[:10]
                )
                else 0.0
            )
            for instrument, volume in self.positions.items()
        }
        return AccountSnapshot(
            account_id=account_id,
            as_of=session,
            balance=equity,
            available=self.cash,
            positions=dict(self.positions),
            sellable=sellable,
        )


class _ReplayBroker:
    """Deterministic broker/account ports used by the shared execution FSM."""

    def __init__(
        self,
        account: _ReplayAccount,
        config: ReplayConfig,
        store: StrategyRuntimeStore,
        *,
        orders: list[dict[str, Any]],
        fills: list[dict[str, Any]],
    ) -> None:
        self.account = account
        self.config = config
        self.store = store
        self.orders_output = orders
        self.fills_output = fills
        self.session = ""
        self.prices: dict[str, float] = {}
        self.instruments: dict[str, InstrumentMetadata] = {}
        self.orders: dict[str, dict[str, Any]] = {}

    def set_session(
        self,
        session: str,
        prices: Mapping[str, float],
        instruments: Mapping[str, InstrumentMetadata],
    ) -> None:
        self.session = str(session)
        self.prices = {canonical_instrument(key): float(value) for key, value in prices.items()}
        self.instruments = {
            canonical_instrument(key): value for key, value in instruments.items()
        }
        self.account.settlement_days.update({
            instrument: max(int(metadata.settlement_days), 0)
            for instrument, metadata in self.instruments.items()
        })

    def account_snapshot(self) -> AccountSnapshot:
        base = self.account.snapshot(self.session, self.prices)
        active: dict[str, float] = {}
        for order in self.orders.values():
            if order["status"] not in {"submitted", "nottraded", "parttraded"}:
                continue
            remaining = float(order["volume"]) - float(order["filled"])
            sign = 1.0 if order["side"] == "buy" else -1.0
            instrument = str(order["instrument"])
            active[instrument] = active.get(instrument, 0.0) + sign * remaining
        return AccountSnapshot(
            **{**base.__dict__, "active_order_deltas": active},
        )

    def submit_child(self, child: ExecutionChild) -> str | None:
        if child.reference in self.orders:
            return str(self.orders[child.reference]["order_id"])
        order_id = f"replay-{child.reference[-12:]}"
        row = {
            **asdict(child),
            "session": self.session,
            "order_id": order_id,
            "status": "submitted",
            "filled": 0.0,
            "polls": 0,
        }
        self.orders[child.reference] = row
        self.orders_output.append(row)
        return order_id

    def child_statuses(self, references: Sequence[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for reference in references:
            order = self.orders.get(str(reference))
            if order is None:
                continue
            if order["status"] in {"submitted", "nottraded", "parttraded"}:
                self._advance_order(order)
            result[str(reference)] = str(order["status"])
        return result

    def cancel_child(self, reference: str) -> bool:
        order = self.orders.get(str(reference))
        if order is None or order["status"] in {"alltraded", "cancelled", "rejected"}:
            return True
        order["status"] = "cancelled"
        return True

    def _advance_order(self, order: dict[str, Any]) -> None:
        order["polls"] = int(order.get("polls") or 0) + 1
        ratio = min(max(float(self.config.partial_fill_ratio), 0.0), 1.0)
        remaining = float(order["volume"]) - float(order["filled"])
        if remaining <= 0:
            order["status"] = "alltraded"
            return
        if ratio <= 0:
            order["status"] = "nottraded" if order["polls"] == 1 else "cancelled"
            return
        volume = _floor_lot(remaining * ratio, self.config.lot_size)
        if volume <= 0:
            volume = remaining
        price = float(order["price"]) * (
            1.0 + self.config.slippage
            if order["side"] == "buy"
            else 1.0 - self.config.slippage
        )
        fee_rate = self.config.open_cost if order["side"] == "buy" else self.config.close_cost
        if order["side"] == "buy":
            affordable = _floor_lot(
                max(self.account.cash - self.config.min_cost, 0.0)
                / max(price * (1.0 + fee_rate), 1e-12),
                self.config.lot_size,
            )
            volume = min(volume, affordable)
        else:
            sellable = self.account.snapshot(self.session, self.prices).sellable.get(
                str(order["instrument"]), 0.0,
            )
            volume = min(volume, float(sellable))
        if volume <= 0:
            order["status"] = "rejected"
            return
        value = volume * price
        fee = max(value * fee_rate, self.config.min_cost)
        instrument = str(order["instrument"])
        if order["side"] == "buy":
            self.account.cash -= value + fee
            self.account.positions[instrument] = self.account.positions.get(instrument, 0.0) + volume
            self.account.acquired_session[instrument] = self.session
        else:
            self.account.cash += value - fee
            left = self.account.positions.get(instrument, 0.0) - volume
            if left > 0:
                self.account.positions[instrument] = left
            else:
                self.account.positions.pop(instrument, None)
                self.account.acquired_session.pop(instrument, None)
        order["filled"] = float(order["filled"]) + volume
        order["status"] = (
            "alltraded"
            if float(order["filled"]) >= float(order["volume"]) - 1e-9
            else "parttraded"
        )
        fill = {
            "reference": order["reference"],
            "session": self.session,
            "instrument": instrument,
            "side": order["side"],
            "volume": volume,
            "price": price,
            "fee": fee,
        }
        self.fills_output.append(fill)
        child_row = self.store.get_child_order(str(order["reference"]))
        if child_row is not None:
            self.store.record_fill_reconciliation(
                f"{order['reference']}:{order['filled']}:{price}",
                str(child_row["plan_id"]),
                str(order["reference"]),
                order_id=str(order["order_id"]),
                volume=volume,
                price=price,
                payload=fill,
            )


class ReplayRuntime:
    """Replay completed feature bars and raw D+1 quotes through one pipeline."""

    def __init__(
        self,
        *,
        strategy_registry: Any,
        policy_registry: Any,
        store: Any,
        output_root: str | Path,
    ) -> None:
        self.strategy_registry = strategy_registry
        self.policy_registry = policy_registry
        self.store = store
        self.output_root = Path(output_root).expanduser()

    def run(
        self,
        run_id: str,
        instance: StrategyInstanceConfig,
        feature_bars: Sequence[CompletedBar],
        raw_bars: Sequence[CompletedBar],
        *,
        config: ReplayConfig | None = None,
    ) -> ReplayResult:
        options = config or ReplayConfig()
        self._validate_inputs(instance, feature_bars, raw_bars)
        artifact_dir = self.output_root / run_id
        artifact_dir.mkdir(parents=True, exist_ok=False)
        run_store = StrategyRuntimeStore(artifact_dir / "runtime.sqlite3")
        run_store.create_instance(instance)
        sessions = sorted({_bar_key(bar, instance.frequency) for bar in raw_bars})
        if len(sessions) < 2:
            raise ValueError("replay requires at least two completed-bar timestamps")
        calendar = SequenceCalendar(sessions)
        pipeline = DecisionPipeline(
            strategy_registry=self.strategy_registry,
            policy_registry=self.policy_registry,
            store=run_store,
            calendar=calendar,
        )
        planner = ExecutionPlanner(
            lot_size=options.lot_size,
            max_order_value=options.max_order_value,
        )
        raw_by_session = _group_bars(raw_bars, instance.frequency)
        feature_by_session = _group_bars(feature_bars, instance.frequency)
        account = _ReplayAccount(float(options.initial_cash))
        signals: list[dict[str, Any]] = []
        weights: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        plans: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        fills: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        equity: list[dict[str, Any]] = []
        replay_broker = _ReplayBroker(
            account,
            options,
            run_store,
            orders=orders,
            fills=fills,
        )
        history: list[CompletedBar] = []
        valuation_prices: dict[str, float] = {}
        for session in sessions:
            raw_session = raw_by_session.get(session, {})
            mark_prices = {key: float(bar.close) for key, bar in raw_session.items()}
            valuation_prices.update(mark_prices)
            snapshot = account.snapshot(session, valuation_prices)
            equity.append({"session": session, "equity": snapshot.balance, "cash": account.cash})
            for instrument, volume in sorted(account.positions.items()):
                positions.append({
                    "session": session,
                    "instrument": instrument,
                    "volume": volume,
                    "price": valuation_prices.get(instrument, 0.0),
                })
            history.extend(feature_by_session.get(session, {}).values())
            if session == sessions[-1]:
                continue
            try:
                result = pipeline.evaluate(
                    instance,
                    history,
                    account=snapshot,
                    persist=True,
                    mode="replay",
                    run_id=run_id,
                )
            except WarmupRequired:
                continue
            decision = result.decision
            signals.append(decision.signal.to_dict())
            weights.append({
                "decision_id": decision.decision_id,
                "as_of": decision.as_of,
                **decision.target_weights.to_dict(),
            })
            effective = decision.effective_session
            raw_effective = raw_by_session.get(effective, {})
            quotes, metadata = _execution_context(raw_effective, effective, options)
            preopen_prices = {key: bar.open for key, bar in raw_effective.items()}
            execution_prices = {**valuation_prices, **preopen_prices}
            execution_snapshot = account.snapshot(effective, execution_prices)
            try:
                target = pipeline.size(
                    decision,
                    instance,
                    account=execution_snapshot,
                    quotes=quotes,
                    instruments=metadata,
                    session=effective,
                    fees=FeeSchedule(
                        buy_rate=options.open_cost,
                        sell_rate=options.close_cost,
                        min_fee=options.min_cost,
                        max_order_value=options.max_order_value,
                    ),
                )
            except ValueError as exc:
                blocked = _blocked_sizing_plan(decision, str(exc))
                run_store.attach_execution_observation(
                    instance.instance_id,
                    instance.config_hash,
                    mode="replay",
                    run_id=run_id,
                    as_of=decision.as_of,
                    account_hash=canonical_hash(asdict(execution_snapshot)),
                    quote_hash=canonical_hash({
                        str(key): asdict(value) for key, value in quotes.items()
                    }),
                    instrument_hash=canonical_hash({
                        str(key): asdict(value) for key, value in metadata.items()
                    }),
                    plan_hash=canonical_hash(blocked.to_dict()),
                )
                targets.append({
                    "decision_id": decision.decision_id,
                    "instance_id": decision.instance_id,
                    "config_hash": decision.config_hash,
                    "as_of": decision.as_of,
                    "effective_session": decision.effective_session,
                    "status": "blocked",
                    "reason": str(exc),
                })
                plans.append({
                    **blocked.to_dict(),
                    "final_phase": ExecutionPhase.PAUSED.value,
                    "last_error": {
                        "rule": "sizing_blocked",
                        "reason": str(exc),
                    },
                })
                run_store.record_plan(
                    blocked.plan_id,
                    blocked.decision_id,
                    blocked.instance_id,
                    {"plan": blocked.to_dict(), "reason": str(exc)},
                    "blocked",
                )
                run_store.save_execution_plan_state(
                    blocked.plan_id,
                    blocked.decision_id,
                    blocked.instance_id,
                    blocked.config_hash,
                    phase=ExecutionPhase.PAUSED.value,
                    payload={"plan": blocked.to_dict(), "reason": str(exc)},
                    last_error={"rule": "sizing_blocked", "reason": str(exc)},
                )
                run_store.update_decision_status(decision.decision_id, "blocked")
                continue
            targets.append(_target_dict(target))
            plan = planner.plan(target, execution_snapshot, quotes=quotes, instruments=metadata)
            run_store.attach_execution_observation(
                instance.instance_id,
                instance.config_hash,
                mode="replay",
                run_id=run_id,
                as_of=decision.as_of,
                account_hash=canonical_hash(asdict(execution_snapshot)),
                quote_hash=canonical_hash({
                    str(key): asdict(value) for key, value in quotes.items()
                }),
                instrument_hash=canonical_hash({
                    str(key): asdict(value) for key, value in metadata.items()
                }),
                plan_hash=canonical_hash(plan.to_dict()),
            )
            plans.append(plan.to_dict())
            replay_broker.set_session(effective, execution_prices, metadata)
            execution = ExecutionCoordinator(
                store=run_store,
                account_port=replay_broker,
                route_port=replay_broker,
                planner=planner,
                can_route=True,
                expected_account_id="replay",
            )
            state = execution.begin(
                plan,
                target,
                universe=instance.universe,
                quotes=quotes,
                instruments=metadata,
            )
            run_store.update_decision_status(
                decision.decision_id,
                "blocked" if state["phase"] == ExecutionPhase.PAUSED.value else "planned",
            )
            for _ in range(256):
                if state["phase"] in {
                    ExecutionPhase.COMPLETED.value,
                    ExecutionPhase.PAUSED.value,
                    ExecutionPhase.FAILED.value,
                }:
                    break
                state = execution.advance(plan.plan_id)
            else:
                state = execution.pause(plan.plan_id, "replay execution exceeded phase budget")
            plans[-1] = {**plans[-1], "final_phase": state["phase"], "last_error": state["last_error"]}
            run_store.update_decision_status(
                decision.decision_id,
                "completed" if state["phase"] == ExecutionPhase.COMPLETED.value else "blocked",
            )
        final = account.snapshot(sessions[-1], valuation_prices)
        pipeline.close("replay_complete")
        summary = {
            "run_id": run_id,
            "instance_id": instance.instance_id,
            "config_hash": instance.config_hash,
            "initial_cash": float(options.initial_cash),
            "final_equity": float(final.balance),
            "return": float(final.balance / options.initial_cash - 1.0),
            "sessions": len(sessions),
            "decisions": len(signals),
            "plans": len(plans),
            "orders": len(orders),
            "fills": len(fills),
            "blocked_decisions": sum(
                1 for plan in plans if plan.get("final_phase") == ExecutionPhase.PAUSED.value
            ),
        }
        manifest = {
            "run_id": run_id,
            "instance_id": instance.instance_id,
            "config_hash": instance.config_hash,
            "strategy_id": instance.strategy_id,
            "strategy_version": instance.strategy_version,
            "code_hash": instance.strategy_code_hash,
            "model_hash": instance.model_hash,
            "data_version": sorted({bar.data_version for bar in feature_bars}),
            "raw_data_version": sorted({bar.data_version for bar in raw_bars}),
            "policy": dict(instance.portfolio_policy),
            "runtime_journal": "runtime.sqlite3",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        _write_artifacts(
            artifact_dir,
            manifest=manifest,
            summary=summary,
            signals=signals,
            weights=weights,
            targets=targets,
            plans=plans,
            orders=orders,
            fills=fills,
            positions=positions,
            equity=equity,
        )
        return ReplayResult(run_id, summary, artifact_dir, manifest)

    @staticmethod
    def _validate_inputs(
        instance: StrategyInstanceConfig,
        feature: Sequence[CompletedBar],
        raw: Sequence[CompletedBar],
    ) -> None:
        if not feature or not raw:
            raise ValueError("replay requires feature bars and raw execution bars")
        if any(bar.adjustment == PriceAdjustment.NONE for bar in feature) and (
            str(instance.data_policy.get("feature_adjustment") or "backward") != "none"
        ):
            raise ValueError("feature bars do not match declared adjustment")
        if any(bar.adjustment != PriceAdjustment.NONE for bar in raw):
            raise ValueError("sizing, valuation and fills require unadjusted raw bars")
        feature_key_list = [
            (_bar_key(bar, instance.frequency), canonical_instrument(bar.instrument))
            for bar in feature
        ]
        raw_key_list = [
            (_bar_key(bar, instance.frequency), canonical_instrument(bar.instrument))
            for bar in raw
        ]
        feature_keys = set(feature_key_list)
        raw_keys = set(raw_key_list)
        if len(feature_key_list) != len(feature_keys):
            raise ValueError("duplicate feature bar for the same instrument and timestamp")
        if len(raw_key_list) != len(raw_keys):
            raise ValueError("duplicate raw bar for the same instrument and timestamp")
        missing = sorted(feature_keys - raw_keys)
        if missing:
            raise ValueError(f"raw execution bars are missing for {len(missing)} feature rows")
        if len({bar.data_version for bar in feature if bar.data_version}) > 1:
            raise ValueError("feature data version changed within replay")
        if len({bar.data_version for bar in raw if bar.data_version}) > 1:
            raise ValueError("raw data version changed within replay")


def _group_bars(
    bars: Sequence[CompletedBar], frequency: str,
) -> dict[str, dict[str, CompletedBar]]:
    grouped: dict[str, dict[str, CompletedBar]] = {}
    for bar in bars:
        grouped.setdefault(_bar_key(bar, frequency), {})[
            canonical_instrument(bar.instrument)
        ] = bar
    return grouped


def _execution_context(
    bars: Mapping[str, CompletedBar],
    session: str,
    config: ReplayConfig,
) -> tuple[dict[str, TradableQuote], dict[str, InstrumentMetadata]]:
    quotes = {
        instrument: TradableQuote(
            instrument=instrument,
            as_of=(session if "T" in session else f"{session}T09:25:00+08:00"),
            last=bar.close,
            open=bar.open,
            data_version=bar.data_version,
            price_source="raw",
        )
        for instrument, bar in bars.items()
    }
    overrides = {
        canonical_instrument(key): value
        for key, value in (config.quote_overrides.get(session, {}) or {}).items()
    }
    for instrument, quote in overrides.items():
        if instrument not in bars:
            raise ValueError(f"quote override has no aligned raw bar for {instrument} at {session}")
        if str(quote.as_of)[:10] != str(session)[:10]:
            raise ValueError(f"quote override session mismatch for {instrument}")
        quotes[instrument] = quote
    configured = {
        canonical_instrument(key): value
        for key, value in config.instrument_metadata.items()
    }
    metadata = {}
    for instrument in bars:
        declared = configured.get(instrument)
        metadata[instrument] = declared or InstrumentMetadata(
            instrument=instrument,
            lot_size=config.lot_size,
            settlement_days=1,
        )
    return quotes, metadata


def _target_dict(target: Any) -> dict[str, Any]:
    return {
        "date": target.date,
        "holdings": dict(target.holdings),
        "prices": dict(target.prices),
        "decision_id": target.decision_id,
        "instance_id": target.instance_id,
        "config_hash": target.config_hash,
        "as_of": target.as_of,
        "effective_session": target.effective_session,
        "valid_until": target.valid_until,
        "target_weights": dict(target.target_weights),
        "price_source": target.price_source,
    }


def _blocked_sizing_plan(decision: PortfolioDecision, reason: str) -> ExecutionPlan:
    plan_id = hashlib.sha256(
        f"blocked:{decision.decision_id}:{reason}".encode("utf-8")
    ).hexdigest()[:24]
    return ExecutionPlan(
        plan_id=plan_id,
        decision_id=decision.decision_id,
        instance_id=decision.instance_id,
        config_hash=decision.config_hash,
        phase=ExecutionPhase.PAUSED,
        issues=(PlanIssue("sizing_blocked", reason),),
    )


def _write_artifacts(root: Path, **artifacts: Any) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("manifest", "summary"):
        (root / f"{name}.json").write_text(
            json.dumps(artifacts[name], ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    for name in (
        "signals", "weights", "targets", "plans", "orders", "fills", "positions", "equity",
    ):
        rows = artifacts[name]
        (root / f"{name}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        if name in {"orders", "fills", "positions", "equity"}:
            pd.DataFrame(rows).to_csv(root / f"{name}.csv", index=False)


def _floor_lot(value: float, lot: int) -> float:
    return float(math.floor(max(float(value), 0.0) / lot) * lot) if lot > 0 else float(math.floor(value))


def _bar_key(bar: CompletedBar, frequency: str) -> str:
    return bar.datetime[:10] if str(frequency) == "day" else str(bar.datetime)
