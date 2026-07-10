"""Reusable, broker-agnostic live-trading subsystem.

Public surface is the normalized domain (:mod:`.types`), the broker port
(:mod:`.gateway`) and the config (:mod:`.config`). Concrete brokers, OMS, FSMs,
risk gate and executor live in submodules and are imported on demand so that
``import alphapilot.systems.live`` stays light and free of vn.py / broker SDKs.
"""

from alphapilot.systems.live.config import LiveConfig, RiskLimits, RunMode
from alphapilot.systems.live.events import LiveEvent, LiveEventBus
from alphapilot.systems.live.gateway import BrokerGateway, GatewayCallback, QuoteGateway
from alphapilot.systems.live.plugin import (
    PLUGIN_API_VERSION,
    GatewayCapabilities,
    LivePluginSpec,
    ProviderSpec,
    QuoteChannelSpec,
    SettingField,
    TradeChannelSpec,
)
from alphapilot.systems.live.types import (
    ACTIVE_STATUSES,
    Account,
    CancelRequest,
    Contract,
    Direction,
    Exchange,
    Offset,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Product,
    TickData,
    Trade,
    infer_exchange,
    is_active,
    normalize_symbol,
    symbol_key,
)

__all__ = [
    "ACTIVE_STATUSES",
    "Account",
    "BrokerGateway",
    "CancelRequest",
    "Contract",
    "Direction",
    "Exchange",
    "GatewayCallback",
    "LiveEvent",
    "LiveEventBus",
    "LiveConfig",
    "Offset",
    "Order",
    "OrderRequest",
    "OrderStatus",
    "OrderType",
    "Position",
    "Product",
    "ProviderSpec",
    "QuoteChannelSpec",
    "QuoteGateway",
    "GatewayCapabilities",
    "LivePluginSpec",
    "PLUGIN_API_VERSION",
    "RiskLimits",
    "RunMode",
    "TickData",
    "Trade",
    "TradeChannelSpec",
    "SettingField",
    "infer_exchange",
    "is_active",
    "normalize_symbol",
    "symbol_key",
]
