"""Dynamic registry for pip-installed live trade and quote providers.

Third-party distributions register a lightweight manifest under the
``alphapilot.live.plugins`` entry-point group.  Discovery is intentionally
cached for the life of the process: installing or removing a plugin requires a
Portal/daemon restart, which also prevents gateway code changing underneath a
running live session.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Mapping

from alphapilot.systems.live.plugin import (
    ACCOUNT_KINDS,
    DATA_KINDS,
    PLUGIN_API_VERSION,
    PLUGIN_ENTRY_POINT_GROUP,
    QUOTE_ROLE,
    TRADE_ROLE,
    EndpointSpec,
    GatewayCapabilities,
    LivePluginSpec,
    PluginLoadIssue,
    ProviderSpec,
    SettingField,
)

ENV_PREFIX = "ALPHAPILOT_LIVE_"
BrokerCapabilities = GatewayCapabilities

# Compatibility defaults for callers that still construct BrokerSpec directly.
_COMMON_FIELDS: tuple[SettingField, ...] = (
    SettingField("ACCOUNT", "账号", required=True),
    SettingField("PASSWORD", "密码", required=True),
    SettingField("CLIENT_ID", "客户号", int, 1),
    SettingField("QUOTE_HOST", "行情地址", required=True),
    SettingField("QUOTE_PORT", "行情端口", int, 0, required=True),
    SettingField("TRADE_HOST", "交易地址", required=True),
    SettingField("TRADE_PORT", "交易端口", int, 0, required=True),
    SettingField("QUOTE_PROTOCOL", "行情协议", str, "TCP"),
    SettingField("LOG_LEVEL", "日志级别", str, "INFO"),
)


@dataclass(frozen=True)
class BrokerSpec:
    """Resolved channel record used by the existing live control plane."""

    name: str
    gateway_path: str
    gateway_name: str
    setting_fields: tuple[SettingField, ...] = field(default=_COMMON_FIELDS)
    description: str = ""
    capabilities: GatewayCapabilities = field(default_factory=GatewayCapabilities)
    endpoints: tuple[EndpointSpec, ...] = ()
    roles: frozenset[str] = field(default_factory=lambda: frozenset({TRADE_ROLE, QUOTE_ROLE}))
    shareable: bool = False
    availability_path: str | None = None
    factory_accepts_roles: bool = False
    plugin_id: str = "manual"
    distribution: str = ""
    version: str = ""
    account_kind: str = "live"
    data_kind: str = "realtime"


_BROKERS: dict[str, BrokerSpec] = {}
_QUOTE_PROVIDERS: dict[str, BrokerSpec] = {}
_PLUGIN_ROWS: list[dict[str, Any]] = []
_PLUGIN_ISSUES: list[PluginLoadIssue] = []
_CONFLICTED: dict[str, set[str]] = {TRADE_ROLE: set(), QUOTE_ROLE: set()}
_DISCOVERED = False
_PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _paper_quote_spec() -> BrokerSpec:
    return BrokerSpec(
        name="paper",
        gateway_path="alphapilot.systems.live.brokers.registry:_make_noop_quote_gateway",
        gateway_name="PAPER",
        setting_fields=(),
        description="In-process PaperBroker quote sandbox",
        capabilities=GatewayCapabilities(asset_classes=("stock",), supports_tick=True),
        roles=frozenset({QUOTE_ROLE}),
        factory_accepts_roles=True,
        plugin_id="alphapilot-core",
        distribution="alphapilot",
        account_kind="local",
        data_kind="synthetic",
    )


def _reset_registry() -> None:
    global _DISCOVERED
    _BROKERS.clear()
    _QUOTE_PROVIDERS.clear()
    _QUOTE_PROVIDERS["paper"] = _paper_quote_spec()
    _PLUGIN_ROWS.clear()
    _PLUGIN_ISSUES.clear()
    _CONFLICTED[TRADE_ROLE].clear()
    _CONFLICTED[QUOTE_ROLE].clear()
    _DISCOVERED = False


_reset_registry()


def reset_plugin_registry_for_tests() -> None:
    """Clear the process cache. Intended for isolated discovery tests only."""

    _reset_registry()


def _load_path(path: str) -> Any:
    module_path, attr_name = path.split(":", 1)
    return getattr(import_module(module_path), attr_name)


def _entry_points() -> list[Any]:
    from importlib.metadata import entry_points

    try:
        return list(entry_points(group=PLUGIN_ENTRY_POINT_GROUP))
    except TypeError:  # pragma: no cover - old importlib-metadata
        return list(entry_points().get(PLUGIN_ENTRY_POINT_GROUP, []))  # type: ignore[attr-defined]


def _dist_info(ep: Any) -> tuple[str, str]:
    dist = getattr(ep, "dist", None)
    if dist is None:
        return "", ""
    try:
        return str(dist.metadata.get("Name") or ""), str(dist.version or "")
    except Exception:  # pragma: no cover - third-party metadata edge case
        return "", ""


def _issue(plugin_id: str, kind: str, error: str, distribution: str = "", version: str = "") -> None:
    _PLUGIN_ISSUES.append(
        PluginLoadIssue(
            plugin_id=plugin_id,
            kind=kind,
            error=str(error),
            distribution=distribution,
            version=version,
        )
    )


def _channel_record(
    plugin: LivePluginSpec,
    provider: ProviderSpec,
    *,
    role: str,
    distribution: str,
    version: str,
) -> BrokerSpec:
    channel = provider.trade if role == TRADE_ROLE else provider.quote
    if channel is None:  # pragma: no cover - caller checks this
        raise ValueError(f"provider {provider.name!r} has no {role} channel")
    return BrokerSpec(
        name=provider.name.lower(),
        gateway_path=provider.factory_path,
        gateway_name=provider.gateway_name,
        setting_fields=channel.setting_fields,
        description=provider.description or plugin.description,
        capabilities=channel.capabilities,
        endpoints=channel.endpoints,
        roles=provider.roles,
        shareable=provider.shareable,
        availability_path=provider.availability_path,
        factory_accepts_roles=True,
        plugin_id=plugin.plugin_id,
        distribution=distribution,
        version=version,
        account_kind=(channel.account_kind if role == TRADE_ROLE else "local"),
        data_kind=(channel.data_kind if role == QUOTE_ROLE else "realtime"),
    )


def _register_channel(spec: BrokerSpec, *, role: str) -> None:
    registry = _BROKERS if role == TRADE_ROLE else _QUOTE_PROVIDERS
    name = spec.name
    if name == "paper":
        _issue(spec.plugin_id, "reserved_name", "provider name 'paper' is reserved", spec.distribution, spec.version)
        return
    if name in _CONFLICTED[role]:
        _issue(spec.plugin_id, "duplicate_provider", f"duplicate {role} provider {name!r}", spec.distribution, spec.version)
        return
    existing = registry.get(name)
    if existing is not None:
        registry.pop(name, None)
        _CONFLICTED[role].add(name)
        _issue(existing.plugin_id, "duplicate_provider", f"duplicate {role} provider {name!r}", existing.distribution, existing.version)
        _issue(spec.plugin_id, "duplicate_provider", f"duplicate {role} provider {name!r}", spec.distribution, spec.version)
        return
    registry[name] = spec


def _register_plugin(plugin: LivePluginSpec, *, entry_name: str, distribution: str, version: str) -> None:
    if not isinstance(plugin, LivePluginSpec):
        raise TypeError("entry point must return LivePluginSpec")
    if plugin.api_version != PLUGIN_API_VERSION:
        raise ValueError(
            f"unsupported live plugin API {plugin.api_version}; AlphaPilot supports {PLUGIN_API_VERSION}"
        )
    if plugin.plugin_id != entry_name:
        raise ValueError(f"entry point name {entry_name!r} does not match plugin_id {plugin.plugin_id!r}")
    if not plugin.providers:
        raise ValueError("plugin must provide at least one provider")

    provider_rows: list[dict[str, Any]] = []
    for provider in plugin.providers:
        name = provider.name.lower()
        if provider.name != name or not _PROVIDER_NAME.fullmatch(name):
            raise ValueError(f"invalid provider name {provider.name!r}")
        if not provider.roles:
            raise ValueError(f"provider {name!r} has no trade or quote role")
        if provider.trade is not None and provider.trade.account_kind not in ACCOUNT_KINDS:
            raise ValueError(
                f"provider {name!r} has invalid account_kind {provider.trade.account_kind!r}"
            )
        if provider.quote is not None and provider.quote.data_kind not in DATA_KINDS:
            raise ValueError(
                f"provider {name!r} has invalid data_kind {provider.quote.data_kind!r}"
            )
        if provider.trade is not None:
            _register_channel(
                _channel_record(plugin, provider, role=TRADE_ROLE, distribution=distribution, version=version),
                role=TRADE_ROLE,
            )
        if provider.quote is not None:
            _register_channel(
                _channel_record(plugin, provider, role=QUOTE_ROLE, distribution=distribution, version=version),
                role=QUOTE_ROLE,
            )
        provider_rows.append({"name": name, "roles": sorted(provider.roles)})

    _PLUGIN_ROWS.append(
        {
            "plugin_id": plugin.plugin_id,
            "description": plugin.description,
            "api_version": plugin.api_version,
            "distribution": distribution,
            "version": version,
            "status": "loaded",
            "providers": provider_rows,
        }
    )


def discover_plugins() -> None:
    """Discover live plugins once for the current process."""

    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    for ep in _entry_points():
        distribution, version = _dist_info(ep)
        plugin_id = str(getattr(ep, "name", "unknown"))
        try:
            loaded = ep.load()
            plugin = loaded() if callable(loaded) else loaded
            _register_plugin(
                plugin,
                entry_name=plugin_id,
                distribution=distribution,
                version=version,
            )
        except Exception as exc:  # noqa: BLE001 - isolate third-party plugins
            _issue(plugin_id, "load_error", f"{type(exc).__name__}: {exc}", distribution, version)
            _PLUGIN_ROWS.append(
                {
                    "plugin_id": plugin_id,
                    "api_version": None,
                    "distribution": distribution,
                    "version": version,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "providers": [],
                }
            )


def plugin_diagnostics() -> dict[str, Any]:
    discover_plugins()
    return {
        "api_version": PLUGIN_API_VERSION,
        "entry_point_group": PLUGIN_ENTRY_POINT_GROUP,
        "plugins": list(_PLUGIN_ROWS),
        "issues": [
            {
                "plugin_id": issue.plugin_id,
                "kind": issue.kind,
                "error": issue.error,
                "distribution": issue.distribution,
                "version": issue.version,
            }
            for issue in _PLUGIN_ISSUES
        ],
    }


def register_plugin_spec(
    plugin: LivePluginSpec,
    *,
    distribution: str = "manual-plugin",
    version: str = "dev",
) -> None:
    """Register a manifest explicitly for embedding and deterministic tests."""

    discover_plugins()
    _register_plugin(
        plugin,
        entry_name=plugin.plugin_id,
        distribution=distribution,
        version=version,
    )


def register_broker(spec: BrokerSpec) -> None:
    """Register a legacy in-process broker (entry points are preferred)."""

    name = spec.name.lower()
    _BROKERS[name] = spec
    if QUOTE_ROLE in spec.roles:
        _QUOTE_PROVIDERS[name] = spec


def register_quote_provider(spec: BrokerSpec) -> None:
    _QUOTE_PROVIDERS[spec.name.lower()] = spec


def get_broker(name: str) -> BrokerSpec:
    discover_plugins()
    spec = _BROKERS.get(str(name).lower())
    if spec is None:
        raise ValueError(f"unknown trade broker {name!r}; registered: {sorted(_BROKERS)}")
    return spec


def get_quote_provider(name: str) -> BrokerSpec:
    discover_plugins()
    spec = _QUOTE_PROVIDERS.get(str(name).lower())
    if spec is None:
        raise ValueError(f"unknown quote provider {name!r}; registered: {sorted(_QUOTE_PROVIDERS)}")
    return spec


def list_brokers(*, account_kind: str | None = None) -> list[BrokerSpec]:
    discover_plugins()
    rows = [_BROKERS[key] for key in sorted(_BROKERS)]
    return rows if account_kind is None else [row for row in rows if row.account_kind == account_kind]


def list_quote_providers(*, data_kind: str | None = None) -> list[BrokerSpec]:
    discover_plugins()
    rows = [_QUOTE_PROVIDERS[key] for key in sorted(_QUOTE_PROVIDERS)]
    return rows if data_kind is None else [row for row in rows if row.data_kind == data_kind]


def validate_provider_pair(mode: str, trade_name: str, quote_name: str) -> tuple[BrokerSpec, BrokerSpec]:
    """Validate environment/provider roles before any native SDK is imported."""

    from alphapilot.systems.live.config import RunMode

    trade = get_broker(trade_name)
    quote = get_quote_provider(quote_name)
    if mode == RunMode.SIMULATION and trade.account_kind != "simulation":
        raise ValueError(
            f"SIMULATION requires a simulation trade provider; {trade.name!r} is {trade.account_kind!r}"
        )
    if mode in {RunMode.LIVE, RunMode.SHADOW} and trade.account_kind != "live":
        raise ValueError(
            f"{mode.upper()} requires a live trade provider; {trade.name!r} is {trade.account_kind!r}"
        )
    if mode in {RunMode.LIVE, RunMode.SHADOW} and quote.data_kind != "realtime":
        raise ValueError(f"{mode.upper()} requires realtime quotes; {quote.name!r} is {quote.data_kind!r}")
    return trade, quote


def resolve_gateway_class(name: str) -> Any:
    """Compatibility resolver for a trade provider's lazy factory/class path."""

    spec = get_broker(name)
    try:
        return _load_path(spec.gateway_path)
    except (ImportError, AttributeError, ValueError) as exc:
        raise ImportError(
            f"trade broker {spec.name!r} from {spec.distribution or spec.plugin_id!r} "
            f"cannot load {spec.gateway_path!r}: {exc}"
        ) from exc


def _availability(spec: BrokerSpec) -> tuple[bool, str]:
    try:
        _load_path(spec.gateway_path)
        if not spec.availability_path:
            return True, "available"
        result = _load_path(spec.availability_path)()
        if isinstance(result, Mapping):
            return bool(result.get("ok")), str(result.get("detail") or result.get("error") or "")
        if isinstance(result, tuple):
            return bool(result[0]), str(result[1] if len(result) > 1 else "")
        return bool(result), "available" if result else "availability probe failed"
    except Exception as exc:  # noqa: BLE001 - catalog probe must be best-effort
        return False, f"{type(exc).__name__}: {exc}"


def provider_availability(name: str, *, role: str) -> tuple[bool, str]:
    spec = get_broker(name) if role == TRADE_ROLE else get_quote_provider(name)
    return _availability(spec)


def gateway_importable(name: str) -> bool:
    try:
        return _availability(get_broker(name))[0]
    except Exception:  # noqa: BLE001
        return False


def quote_provider_importable(name: str) -> bool:
    try:
        return _availability(get_quote_provider(name))[0]
    except Exception:  # noqa: BLE001
        return False


def _instantiate(spec: BrokerSpec, roles: frozenset[str]) -> Any:
    factory = _load_path(spec.gateway_path)
    if spec.factory_accepts_roles:
        return factory(name=spec.name, roles=roles)

    from alphapilot.systems.live.gateway import BrokerGateway

    if isinstance(factory, type) and issubclass(factory, BrokerGateway):
        return factory(spec.name)
    from alphapilot.systems.live.brokers.vnpy_adapter import VnpyBrokerAdapter

    # Legacy BrokerSpec entries represent vn.py-style combined gateways. The
    # adapter satisfies both the trade ABC and quote protocol.
    return VnpyBrokerAdapter(spec.gateway_name, gateway_class=factory)


def _validate_gateway(gateway: Any, *, role: str, name: str) -> None:
    from alphapilot.systems.live.gateway import BrokerGateway, QuoteGateway

    if role == TRADE_ROLE and not isinstance(gateway, BrokerGateway):
        raise TypeError(f"provider {name!r} factory did not return BrokerGateway")
    if role == QUOTE_ROLE and not isinstance(gateway, QuoteGateway):
        raise TypeError(f"provider {name!r} factory did not return QuoteGateway")


def create_gateway(name: str):
    spec = get_broker(name)
    gateway = _instantiate(spec, frozenset({TRADE_ROLE}))
    _validate_gateway(gateway, role=TRADE_ROLE, name=spec.name)
    return gateway


def create_quote_gateway(name: str):
    spec = get_quote_provider(name)
    gateway = _instantiate(spec, frozenset({QUOTE_ROLE}))
    _validate_gateway(gateway, role=QUOTE_ROLE, name=spec.name)
    return gateway


def create_gateway_pair(trade_name: str, quote_name: str):
    """Create a role-correct trade/quote pair, sharing one instance when declared."""

    trade = get_broker(trade_name)
    quote = get_quote_provider(quote_name)
    if (
        trade.name == quote.name
        and trade.plugin_id == quote.plugin_id
        and trade.gateway_path == quote.gateway_path
        and trade.shareable
        and quote.shareable
    ):
        gateway = _instantiate(trade, frozenset({TRADE_ROLE, QUOTE_ROLE}))
        _validate_gateway(gateway, role=TRADE_ROLE, name=trade.name)
        _validate_gateway(gateway, role=QUOTE_ROLE, name=quote.name)
        return gateway, gateway
    trade_gateway = _instantiate(trade, frozenset({TRADE_ROLE}))
    quote_gateway = _instantiate(quote, frozenset({QUOTE_ROLE}))
    _validate_gateway(trade_gateway, role=TRADE_ROLE, name=trade.name)
    _validate_gateway(quote_gateway, role=QUOTE_ROLE, name=quote.name)
    return trade_gateway, quote_gateway


def provider_pair_metadata(trade_name: str, quote_name: str) -> dict[str, Any]:
    """Return a non-secret fingerprint of the selected plugin implementations."""

    trade = get_broker(trade_name)
    quote = get_quote_provider(quote_name)

    def row(spec: BrokerSpec, role: str) -> dict[str, Any]:
        available, detail = _availability(spec)
        return {
            "name": spec.name,
            "role": role,
            "plugin_id": spec.plugin_id,
            "distribution": spec.distribution,
            "version": spec.version,
            "available": available,
            "availability_detail": detail,
            "account_kind": spec.account_kind,
            "data_kind": spec.data_kind,
        }

    return {"trade": row(trade, TRADE_ROLE), "quote": row(quote, QUOTE_ROLE)}


def _make_noop_quote_gateway(*, name: str = "paper", roles: frozenset[str] = frozenset({QUOTE_ROLE})):
    del roles
    return _NoopQuoteGateway(name)


class _NoopQuoteGateway:
    def __init__(self, name: str = "paper") -> None:
        self.name = name
        self._callback = None

    def register_callback(self, callback) -> None:  # noqa: ANN001
        self._callback = callback

    def connect(self, setting: dict) -> None:  # noqa: ARG002
        handler = getattr(self._callback, "on_gateway_connected", None)
        if handler is not None:
            handler(self.name, "quote", "noop")

    def close(self) -> None:
        handler = getattr(self._callback, "on_gateway_disconnected", None)
        if handler is not None:
            handler(self.name, "quote", "noop", halt=False)

    def subscribe(self, codes: list[str]) -> None:
        del codes


def _setting_json(spec: BrokerSpec, role: str, env: Mapping[str, str]) -> str | None:
    prefix = f"{ENV_PREFIX}{spec.name.upper()}_"
    return env.get(f"{prefix}{role.upper()}_SETTING_JSON") or env.get(f"{prefix}SETTING_JSON")


def _build_setting(spec: BrokerSpec, *, role: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = os.environ if env is None else env
    prefix = f"{ENV_PREFIX}{spec.name.upper()}_"
    raw_json = _setting_json(spec, role, source)
    if raw_json:
        parsed = json.loads(raw_json)
        if not isinstance(parsed, dict):
            raise ValueError(f"{prefix}{role.upper()}_SETTING_JSON must be a JSON object")
        return parsed
    setting: dict[str, Any] = {}
    for item in spec.setting_fields:
        raw = source.get(prefix + item.env_suffix)
        setting[item.gateway_key] = item.default if raw is None or raw == "" else item.cast(raw)
    return setting


def _missing_fields(spec: BrokerSpec, *, role: str, env: Mapping[str, str] | None = None) -> list[str]:
    source = os.environ if env is None else env
    prefix = f"{ENV_PREFIX}{spec.name.upper()}_"
    if _setting_json(spec, role, source):
        return []
    return [
        prefix + item.env_suffix
        for item in spec.setting_fields
        if item.is_required and not source.get(prefix + item.env_suffix)
    ]


def build_connect_setting(name: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return _build_setting(get_broker(name), role=TRADE_ROLE, env=env)


def build_quote_connect_setting(name: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    return _build_setting(get_quote_provider(name), role=QUOTE_ROLE, env=env)


def missing_setting_fields(name: str, env: Mapping[str, str] | None = None) -> list[str]:
    return _missing_fields(get_broker(name), role=TRADE_ROLE, env=env)


def missing_quote_setting_fields(name: str, env: Mapping[str, str] | None = None) -> list[str]:
    return _missing_fields(get_quote_provider(name), role=QUOTE_ROLE, env=env)
