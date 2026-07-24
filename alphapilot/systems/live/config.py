"""Configuration for the live-trading subsystem.

Dependency-light dataclass + environment overrides, matching the style of
:mod:`alphapilot.kernel.config`. Read standalone via :meth:`LiveConfig.load` (the
live process reads it directly), and later surfaced on ``AppConfig.live`` for the
CLI / portal.

Security note: broker credentials are **never** stored here with defaults — they
are pulled from the environment (or a secret store) at connect time by the
gateway. This object only carries non-secret knobs: run mode, which broker,
risk limits, session/calendar and ledger location.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class RunMode:
    """Low-level runtime modes (see :mod:`alphapilot.systems.live.fsm.runmode_fsm`)."""

    DRY_RUN = "dry_run"   # compute + print intents, submit nothing
    PAPER = "paper"       # route to the in-process PaperBroker
    SIMULATION = "simulation"  # route to an external stateful simulation counter
    SHADOW = "shadow"     # real account/quotes, planning only; routing is impossible
    LIVE = "live"         # route to a real broker gateway


def uses_real_providers(mode: str) -> bool:
    """Whether the mode connects to configured broker and quote providers."""

    return mode in {RunMode.SIMULATION, RunMode.SHADOW, RunMode.LIVE}


def allows_order_routing(mode: str) -> bool:
    """Whether the run-mode FSM may route a new order."""

    return mode in {RunMode.PAPER, RunMode.SIMULATION, RunMode.LIVE}


def requires_live_market_safety(mode: str) -> bool:
    """Whether planning requires production-grade account/contract/quote truth."""

    return uses_real_providers(mode)


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value else default


@dataclass
class RiskLimits:
    """Pre-trade risk limits enforced by :mod:`alphapilot.systems.live.risk`.

    All monetary limits are in account currency (CNY). ``<= 0`` disables a limit.
    """

    #: Max notional of a single order.
    max_order_value: float = field(default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MAX_ORDER_VALUE", 200_000.0))
    #: Optional dynamic single-order cap as a fraction of current account equity.
    max_order_equity_pct: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MAX_ORDER_EQUITY_PCT", 0.0)
    )
    #: Max total notional traded in one day (buys + sells).
    max_daily_value: float = field(default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MAX_DAILY_VALUE", 2_000_000.0))
    #: Optional dynamic daily turnover cap as a fraction of current account equity.
    max_daily_equity_pct: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MAX_DAILY_EQUITY_PCT", 0.0)
    )
    #: Max fraction of account equity in a single instrument (0..1).
    max_position_pct: float = field(default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MAX_POSITION_PCT", 0.30))
    #: Max aggregate long-equity exposure as a fraction of account equity.
    max_total_position_pct: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MAX_TOTAL_POSITION_PCT", 0.0)
    )
    #: Max number of instruments with holdings or pending buy orders.
    max_position_count: int = field(
        default_factory=lambda: _env_int("ALPHAPILOT_LIVE_MAX_POSITION_COUNT", 0)
    )
    #: Reject a limit price deviating more than this fraction from the reference.
    price_guard_pct: float = field(default_factory=lambda: _env_float("ALPHAPILOT_LIVE_PRICE_GUARD_PCT", 0.05))
    #: Max number of orders accepted in one day (throttle / runaway guard).
    max_orders_per_day: int = field(default_factory=lambda: _env_int("ALPHAPILOT_LIVE_MAX_ORDERS_PER_DAY", 1000))
    #: Board-lot size (A-shares = 100; 0 disables lot rounding).
    lot_size: int = field(default_factory=lambda: _env_int("ALPHAPILOT_LIVE_LOT_SIZE", 100))
    #: LIVE orders require a quote no older than this many seconds.
    max_quote_age_seconds: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MAX_QUOTE_AGE", 3.0)
    )
    #: Conservative fee assumptions used by account sizing before routing.
    buy_fee_rate: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_BUY_FEE_RATE", 0.00015)
    )
    sell_fee_rate: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_SELL_FEE_RATE", 0.00015)
    )
    min_fee: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MIN_FEE", 5.0)
    )
    #: Halt when account equity falls this far from the first observed equity today.
    max_daily_loss_pct: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MAX_DAILY_LOSS_PCT", 0.0)
    )
    #: Halt when equity falls this far from the persisted canary starting equity.
    max_canary_loss_pct: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MAX_CANARY_LOSS_PCT", 0.0)
    )


@dataclass
class MarketDataConfig:
    """Non-secret settings for live quote projection and durable recording."""

    enabled: bool = field(
        default_factory=lambda: _env_bool("ALPHAPILOT_LIVE_MARKET_ENABLED", True)
    )
    data_dir: Path = field(
        default_factory=lambda: _env_path(
            "ALPHAPILOT_LIVE_MARKET_DATA_DIR",
            Path.cwd() / "git_ignore_folder" / "live_market_data",
        )
    )
    retention_days: int = field(
        default_factory=lambda: _env_int("ALPHAPILOT_LIVE_MARKET_RETENTION_DAYS", 30)
    )
    queue_size: int = field(
        default_factory=lambda: _env_int("ALPHAPILOT_LIVE_MARKET_QUEUE_SIZE", 100_000)
    )
    batch_size: int = field(
        default_factory=lambda: _env_int("ALPHAPILOT_LIVE_MARKET_BATCH_SIZE", 1_000)
    )
    flush_interval: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MARKET_FLUSH_INTERVAL", 0.5)
    )
    snapshot_interval: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MARKET_SNAPSHOT_INTERVAL", 1.0)
    )
    stale_after_seconds: float = field(
        default_factory=lambda: _env_float("ALPHAPILOT_LIVE_MARKET_STALE_AFTER", 3.0)
    )


@dataclass
class LiveConfig:
    """Top-level live-trading config."""

    #: One of ``RunMode.{DRY_RUN,PAPER,SIMULATION,SHADOW,LIVE}``.
    mode: str = field(default_factory=lambda: _env("ALPHAPILOT_LIVE_MODE", RunMode.DRY_RUN))
    #: Backward-compatible alias for the trade broker.
    broker: str | None = field(default_factory=lambda: _env("ALPHAPILOT_LIVE_BROKER", "paper"))
    #: Which broker gateway to use for orders/account/positions in LIVE mode.
    trade_broker: str | None = field(default_factory=lambda: os.getenv("ALPHAPILOT_LIVE_TRADE_BROKER"))
    #: Which provider to use for market data subscriptions. Defaults to trade_broker.
    quote_provider: str | None = field(default_factory=lambda: os.getenv("ALPHAPILOT_LIVE_QUOTE_PROVIDER"))
    #: Orthogonal execution environment persisted by formal strategy bindings.
    execution_environment: str = field(
        default_factory=lambda: _env("ALPHAPILOT_EXECUTION_ENVIRONMENT", "")
    )
    #: Resolved from quote-provider metadata at runtime. Never stores credentials.
    quote_data_kind: str = ""
    #: Append-only order/trade audit ledger location.
    ledger_dir: Path = field(
        default_factory=lambda: _env_path(
            "ALPHAPILOT_LIVE_LEDGER_DIR", Path.cwd() / "git_ignore_folder" / "live_ledger"
        )
    )
    #: Rolling live portfolio / reconciliation state location.
    state_dir: Path = field(
        default_factory=lambda: _env_path(
            "ALPHAPILOT_LIVE_STATE_DIR", Path.cwd() / "git_ignore_folder" / "live_state"
        )
    )
    #: IANA timezone for the trading session clock.
    timezone: str = field(default_factory=lambda: _env("ALPHAPILOT_TIMEZONE", "Asia/Shanghai"))
    risk: RiskLimits = field(default_factory=RiskLimits)
    market_data: MarketDataConfig = field(default_factory=MarketDataConfig)

    def __post_init__(self) -> None:
        trade = str(self.trade_broker or self.broker or "paper").strip() or "paper"
        quote = str(self.quote_provider or trade).strip() or trade
        self.trade_broker = trade
        self.quote_provider = quote
        # Keep the public legacy field stable for old CLI/API/tests.
        self.broker = trade
        if not self.execution_environment:
            self.execution_environment = (
                "broker_simulation" if self.mode == RunMode.SIMULATION else
                "local_paper" if self.mode in {RunMode.PAPER, RunMode.DRY_RUN} else
                "live"
            )
        if not self.quote_data_kind and quote == "paper":
            self.quote_data_kind = "synthetic"

    @classmethod
    def load(cls) -> "LiveConfig":
        return cls()

    def summary(self) -> str:
        return (
            "LiveConfig("
            f"mode={self.mode}, trade_broker={self.trade_broker}, "
            f"quote_provider={self.quote_provider}, timezone={self.timezone}, "
            f"execution_environment={self.execution_environment}, "
            f"ledger_dir={self.ledger_dir}, "
            f"market_data_dir={self.market_data.data_dir}, "
            f"risk=[max_order_value={self.risk.max_order_value}, "
            f"max_position_pct={self.risk.max_position_pct}, "
            f"max_total_position_pct={self.risk.max_total_position_pct}, "
            f"max_position_count={self.risk.max_position_count}, "
            f"price_guard_pct={self.risk.price_guard_pct}, lot_size={self.risk.lot_size}]"
            ")"
        )
