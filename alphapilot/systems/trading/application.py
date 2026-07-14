"""Strategy-instance decision pipeline shared by every runtime mode.

This module imports only the dependency-free trading domain.  Live, replay and
data implementations are supplied through ports by outer adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import threading
from typing import Any, Mapping, Sequence

from alphapilot.systems.trading.contracts import (
    AccountSnapshot,
    CompletedBar,
    FeeSchedule,
    InstrumentMetadata,
    PortfolioContext,
    PortfolioDecision,
    PortfolioInputs,
    PriceAdjustment,
    SignalEnvelope,
    SignalKind,
    StrategyEvaluationContext,
    TargetPortfolio,
    TradableQuote,
    canonical_instrument,
)
from alphapilot.systems.trading.domain import StrategyInstanceConfig
from alphapilot.systems.trading.portfolio import AccountSizer
from alphapilot.systems.trading.registry import resolve_required_history


class WarmupRequired(RuntimeError):
    def __init__(self, required: int, available: int) -> None:
        super().__init__(f"strategy requires {required} completed bars per instrument; have {available}")
        self.required = required
        self.available = available


@dataclass(frozen=True)
class DecisionResult:
    signal: SignalEnvelope
    decision: PortfolioDecision
    inserted: bool


class DecisionPipeline:
    def __init__(
        self,
        *,
        strategy_registry: Any,
        policy_registry: Any,
        store: Any,
        calendar: Any,
        sizer: AccountSizer | None = None,
    ) -> None:
        self.strategy_registry = strategy_registry
        self.policy_registry = policy_registry
        self.store = store
        self.calendar = calendar
        self.sizer = sizer or AccountSizer()
        self._providers: dict[tuple[str, str], Any] = {}
        self._provider_lock = threading.RLock()

    def close(self, reason: str = "pipeline_closed") -> None:
        """Stop all session-scoped providers and their isolated workers."""

        with self._provider_lock:
            providers = list(self._providers.values())
            self._providers.clear()
        for provider in providers:
            try:
                provider.stop(reason)
            except Exception:  # noqa: BLE001 - shutdown remains best effort
                pass

    def evaluate(
        self,
        instance: StrategyInstanceConfig,
        bars: Sequence[CompletedBar],
        *,
        account: AccountSnapshot | None = None,
        quotes: Mapping[str, TradableQuote] | None = None,
        instruments: Mapping[str, InstrumentMetadata] | None = None,
        persist: bool = True,
    ) -> DecisionResult:
        definition = self.strategy_registry.get(instance.strategy_id)
        self._validate_instance_binding(instance, definition)
        normalized = self._validate_bars(instance, bars)
        as_of = max(str(bar.datetime) for bar in normalized)
        signal_session = as_of[:10]
        if not self.calendar.is_trading_session(signal_session):
            raise ValueError(f"{signal_session} is not a trading session")
        next_effective = getattr(self.calendar, "next_effective", None)
        effective = (
            next_effective(as_of, instance.frequency)
            if callable(next_effective)
            else self.calendar.next_trading_session(signal_session)
        )
        valid_until_fn = getattr(self.calendar, "valid_until", None)
        valid_until = (
            valid_until_fn(effective, instance.frequency)
            if callable(valid_until_fn)
            else f"{str(effective)[:10]}T15:00:00+08:00"
        )
        required = resolve_required_history(definition, instance.params)
        universe = {canonical_instrument(item) for item in instance.universe}
        counts = {
            instrument: len([bar for bar in normalized if bar.instrument == instrument])
            for instrument in sorted(universe)
        }
        available = min(counts.values(), default=0)
        # Qlib owns feature history, but the point-in-time caller must still
        # supply at least the definition's declared completed-bar watermark.
        if available < required:
            raise WarmupRequired(required, available)
        latest_instruments = {
            bar.instrument for bar in normalized if str(bar.datetime) == as_of
        }
        if latest_instruments != universe:
            missing = sorted(universe - latest_instruments)
            raise ValueError(
                f"latest completed-bar watermark does not cover the universe: {missing}"
            )
        data_versions = {bar.data_version for bar in normalized if bar.data_version}
        data_version = next(iter(data_versions), str(instance.data_policy.get("data_version") or ""))
        context = StrategyEvaluationContext(
            instance_id=instance.instance_id,
            config_hash=instance.config_hash,
            as_of=as_of,
            effective_session=effective,
            frequency=instance.frequency,
            history=tuple(normalized),
            data_version=data_version,
            metadata={"universe": list(instance.universe)},
        )
        provider_key = (instance.instance_id, instance.config_hash)
        with self._provider_lock:
            provider = self._providers.get(provider_key)
            provider_created = provider is None
            if provider is None:
                provider = self.strategy_registry.create_provider(
                    instance.strategy_id,
                    instance.params,
                    factory_context=(
                        {"artifact_binding": dict(instance.artifact_binding)}
                        if instance.artifact_binding else {}
                    ),
                )
                self._providers[provider_key] = provider
        try:
            if provider_created:
                provider.initialize(context)
                checkpoint = self.store.load_provider_checkpoint(
                    instance.instance_id, instance.config_hash,
                )
                if checkpoint is not None:
                    if int(checkpoint["state_schema_version"]) != int(definition.state_schema_version):
                        raise ValueError("provider checkpoint schema version requires an explicit migration")
                    provider.restore(checkpoint["state"])
            provider.warmup(normalized)
            signal = provider.evaluate(context)
            self._validate_signal(signal, instance, definition.signal_kind, as_of, data_version)
            policy_binding = dict(instance.portfolio_policy or {})
            policy_definition = self.policy_registry.get(str(policy_binding.get("policy_id") or ""))
            if definition.signal_kind not in policy_definition.supported_signal_kinds:
                raise ValueError("portfolio policy does not accept the provider signal kind")
            policy = self.policy_registry.create(
                policy_definition.policy_id,
                dict(policy_binding.get("params") or {}),
            )
            policy_account = account or AccountSnapshot(
                account_id="preview",
                as_of=as_of,
                balance=1.0,
                available=1.0,
            )
            portfolio_context = PortfolioContext(
                as_of=as_of,
                account=policy_account,
                quotes=dict(quotes or {}),
                instruments=dict(instruments or {}),
                constraints=dict(instance.portfolio_policy.get("constraints") or {}),
            )
            weights = policy.build(_portfolio_inputs(signal), portfolio_context)
            self._validate_weights(weights.weights, instance.universe)
            decision_id = _stable_id(
                instance.instance_id,
                instance.config_hash,
                as_of,
                effective,
                signal.to_dict(),
                weights.to_dict(),
            )
            decision = PortfolioDecision(
                decision_id=decision_id,
                instance_id=instance.instance_id,
                config_hash=instance.config_hash,
                as_of=as_of,
                effective_session=effective,
                valid_until=valid_until,
                signal=signal,
                target_weights=weights,
                data_version=data_version,
                model_version=signal.model_version,
                strategy_code_hash=instance.strategy_code_hash,
            )
            inserted = False
            if persist:
                signal_id = _stable_id("signal", decision_id)
                self.store.record_signal_envelope(
                    signal_id,
                    instance.instance_id,
                    instance.config_hash,
                    as_of=signal.as_of,
                    signal_kind=signal.kind.value,
                    payload=signal.to_dict(),
                )
                inserted = self.store.record_portfolio_decision(decision.to_dict())
                self.store.save_provider_checkpoint(
                    instance.instance_id,
                    instance.config_hash,
                    state_schema_version=definition.state_schema_version,
                    state=provider.snapshot(),
                )
            return DecisionResult(signal=signal, decision=decision, inserted=inserted)
        except Exception:
            with self._provider_lock:
                self._providers.pop(provider_key, None)
            try:
                provider.stop("evaluation_failed")
            except Exception:  # noqa: BLE001 - preserve the original failure
                pass
            raise

    def size(
        self,
        decision: PortfolioDecision,
        instance: StrategyInstanceConfig,
        *,
        account: AccountSnapshot,
        quotes: Mapping[str, TradableQuote],
        instruments: Mapping[str, InstrumentMetadata],
        session: str,
        fees: FeeSchedule | None = None,
    ) -> TargetPortfolio:
        if decision.instance_id != instance.instance_id or decision.config_hash != instance.config_hash:
            raise ValueError("decision binding does not match the immutable strategy instance")
        if instance.frequency == "day":
            if str(session)[:10] != str(decision.effective_session)[:10]:
                raise ValueError(
                    f"decision is effective on {decision.effective_session}, not {session}"
                )
        elif str(session) < str(decision.effective_session):
            raise ValueError(
                f"decision is not effective until {decision.effective_session}"
            )
        elif decision.valid_until and str(session) > str(decision.valid_until):
            raise ValueError(f"decision expired at {decision.valid_until}")
        if account.external_orders:
            raise ValueError("external active orders require reconciliation before sizing")
        required = {
            canonical_instrument(symbol) for symbol in decision.target_weights.weights
        }
        required.update(
            canonical_instrument(symbol)
            for symbol, volume in account.positions.items() if float(volume)
        )
        required.update(
            canonical_instrument(symbol)
            for symbol, volume in account.active_order_deltas.items() if float(volume)
        )
        quote_keys = {canonical_instrument(key) for key in quotes}
        metadata_keys = {canonical_instrument(key) for key in instruments}
        missing_quotes = sorted(required - quote_keys)
        missing_metadata = sorted(required - metadata_keys)
        if missing_quotes or missing_metadata:
            raise ValueError(
                f"execution data incomplete: quotes={missing_quotes}, metadata={missing_metadata}"
            )
        for key, quote in quotes.items():
            if canonical_instrument(key) in required:
                if quote.price_source != "raw" or quote.stale or quote.suspended:
                    raise ValueError(f"raw tradable quote is unavailable for {key}")
                if str(quote.as_of)[:10] != str(session)[:10]:
                    raise ValueError(f"quote session mismatch for {key}")
        target = self.sizer.size(
            decision.target_weights,
            account,
            {},
            instance,
            quotes=quotes,
            instruments=instruments,
            fees=fees,
            decision_id=decision.decision_id,
            effective_session=decision.effective_session,
            valid_until=decision.valid_until,
        )
        target.data_version = decision.data_version
        target.model_version = decision.model_version
        return target

    @staticmethod
    def _validate_instance_binding(instance: StrategyInstanceConfig, definition: Any) -> None:
        if instance.strategy_version != definition.version:
            raise ValueError("strategy definition version does not match instance")
        if instance.strategy_code_hash != definition.code_hash:
            raise ValueError("strategy code hash does not match instance")
        if instance.frequency not in definition.supported_frequencies:
            raise ValueError("strategy frequency is unsupported")

    @staticmethod
    def _validate_bars(
        instance: StrategyInstanceConfig,
        bars: Sequence[CompletedBar],
    ) -> list[CompletedBar]:
        if not bars:
            raise WarmupRequired(1, 0)
        universe = {canonical_instrument(item) for item in instance.universe}
        expected_adjustment = PriceAdjustment(
            str(instance.data_policy.get("feature_adjustment") or "backward")
        )
        normalized: list[CompletedBar] = []
        versions: set[str] = set()
        seen: set[tuple[str, str]] = set()
        for bar in bars:
            if not isinstance(bar, CompletedBar) or not bar.complete:
                raise ValueError("only typed completed bars may reach a strategy provider")
            instrument = canonical_instrument(bar.instrument)
            if instrument not in universe:
                continue
            if bar.frequency != instance.frequency:
                raise ValueError("bar frequency does not match the strategy instance")
            if bar.adjustment != expected_adjustment:
                raise ValueError("feature-bar adjustment does not match instance data policy")
            identity = (bar.datetime, instrument)
            if identity in seen:
                raise ValueError(f"duplicate completed bar {identity}")
            seen.add(identity)
            versions.add(bar.data_version)
            normalized.append(CompletedBar.from_dict({**bar.to_dict(), "instrument": instrument}))
        if not normalized:
            raise ValueError("completed bars do not cover the instance universe")
        if len({value for value in versions if value}) > 1:
            raise ValueError("completed bars contain multiple data versions")
        return sorted(normalized, key=lambda item: (item.datetime, item.instrument))

    @staticmethod
    def _validate_signal(
        signal: SignalEnvelope,
        instance: StrategyInstanceConfig,
        expected_kind: SignalKind,
        as_of: str,
        data_version: str,
    ) -> None:
        if not isinstance(signal, SignalEnvelope):
            raise TypeError("provider must return SignalEnvelope")
        if signal.kind != expected_kind:
            raise ValueError("provider returned an undeclared signal kind")
        if signal.source_instance_id != instance.instance_id or signal.as_of != as_of:
            raise ValueError("provider signal binding/as_of mismatch")
        if signal.frequency != instance.frequency:
            raise ValueError("provider signal frequency mismatch")
        if data_version and signal.data_version and signal.data_version != data_version:
            raise ValueError("provider signal data version mismatch")

    @staticmethod
    def _validate_weights(weights: Mapping[str, float], universe: Sequence[str]) -> None:
        allowed = {canonical_instrument(item) for item in universe}
        normalized = {canonical_instrument(key): float(value) for key, value in weights.items()}
        if any(value < 0 or value > 1 for value in normalized.values()):
            raise ValueError("target weights must be between zero and one")
        if set(normalized) - allowed:
            raise ValueError("portfolio policy emitted instruments outside the instance universe")
        if sum(normalized.values()) > 1.0 + 1e-9:
            raise ValueError("target weights exceed 100%")


def _portfolio_inputs(signal: SignalEnvelope) -> PortfolioInputs:
    if signal.kind == SignalKind.CROSS_SECTIONAL_SELECTION:
        return PortfolioInputs(selection=signal)
    if signal.kind == SignalKind.INSTRUMENT_TIMING:
        return PortfolioInputs(instrument_timing=(signal,))
    return PortfolioInputs(market_timing=(signal,))


def _stable_id(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]
