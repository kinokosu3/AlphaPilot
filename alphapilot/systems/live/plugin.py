"""Versioned public contract for pip-installed live gateway plugins.

Plugin entry points return :class:`LivePluginSpec` objects.  Specs contain only
metadata and lazy import paths: importing a plugin catalog must not import a
vendor SDK or open a network connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, FrozenSet

PLUGIN_ENTRY_POINT_GROUP = "alphapilot.live.plugins"
PLUGIN_API_VERSION = 1
TRADE_ROLE = "trade"
QUOTE_ROLE = "quote"
GatewayRoles = FrozenSet[str]
ACCOUNT_KINDS = frozenset({"live", "simulation", "local"})
DATA_KINDS = frozenset({"realtime", "replay", "synthetic"})


@dataclass(frozen=True)
class SettingField:
    """Map one environment suffix to a gateway-native connection key."""

    env_suffix: str
    gateway_key: str
    cast: Callable[[str], Any] = str
    default: Any = ""
    required: bool | None = None

    @property
    def is_required(self) -> bool:
        if self.required is None:
            return self.default == ""
        return self.required


@dataclass(frozen=True)
class EndpointSpec:
    """A TCP endpoint that can be checked without logging in."""

    name: str
    host_key: str
    port_key: str


@dataclass(frozen=True)
class GatewayCapabilities:
    """Capability metadata consumed by the CLI, Portal and risk layer."""

    asset_classes: tuple[str, ...] = ("stock", "fund", "bond")
    #: SDK-native products, including products AlphaPilot deliberately blocks.
    native_asset_classes: tuple[str, ...] | None = None
    #: Products the current AlphaPilot route model can actually submit.
    routable_asset_classes: tuple[str, ...] | None = None
    exchanges: tuple[str, ...] = ()
    session_profile: str = "ashare_cash"
    position_model: str = "long_only_t1"
    offset_requirement: str = "none"
    notional_model: str = "cash"
    supports_tick: bool = True
    supports_depth: bool = False
    supports_contract_query: bool = True
    supports_account_query: bool = True
    supports_position_query: bool = True
    supports_order_query: bool = False
    supports_trade_query: bool = False
    supports_cancel: bool = True
    supports_margin: bool = False
    supports_history: bool = False

    @property
    def native_assets(self) -> tuple[str, ...]:
        return self.asset_classes if self.native_asset_classes is None else self.native_asset_classes

    @property
    def routable_assets(self) -> tuple[str, ...]:
        return self.asset_classes if self.routable_asset_classes is None else self.routable_asset_classes


@dataclass(frozen=True)
class TradeChannelSpec:
    """Trade-side metadata for a provider."""

    setting_fields: tuple[SettingField, ...] = ()
    endpoints: tuple[EndpointSpec, ...] = ()
    capabilities: GatewayCapabilities = field(default_factory=GatewayCapabilities)
    account_kind: str = "live"


@dataclass(frozen=True)
class QuoteChannelSpec:
    """Market-data-side metadata for a provider."""

    setting_fields: tuple[SettingField, ...] = ()
    endpoints: tuple[EndpointSpec, ...] = ()
    capabilities: GatewayCapabilities = field(default_factory=GatewayCapabilities)
    data_kind: str = "realtime"


@dataclass(frozen=True)
class ProviderSpec:
    """One selectable provider contributed by a live plugin.

    ``factory_path`` resolves to a callable accepting keyword arguments
    ``name`` and ``roles``.  A shareable provider selected for both roles is
    instantiated once with ``roles=frozenset({"trade", "quote"})``.
    """

    name: str
    factory_path: str
    gateway_name: str
    description: str = ""
    trade: TradeChannelSpec | None = None
    quote: QuoteChannelSpec | None = None
    shareable: bool = False
    availability_path: str | None = None

    @property
    def roles(self) -> frozenset[str]:
        roles: set[str] = set()
        if self.trade is not None:
            roles.add(TRADE_ROLE)
        if self.quote is not None:
            roles.add(QUOTE_ROLE)
        return frozenset(roles)


@dataclass(frozen=True)
class LivePluginSpec:
    """Manifest returned by an ``alphapilot.live.plugins`` entry point."""

    plugin_id: str
    providers: tuple[ProviderSpec, ...]
    api_version: int = PLUGIN_API_VERSION
    description: str = ""


@dataclass(frozen=True)
class PluginLoadIssue:
    """Non-fatal plugin discovery error surfaced through CLI and Portal."""

    plugin_id: str
    kind: str
    error: str
    distribution: str = ""
    version: str = ""
