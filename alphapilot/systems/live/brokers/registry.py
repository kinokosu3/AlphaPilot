"""Broker registry — the one place that knows every supported broker.

Adding a broker later = install its vn.py gateway package + add one
:class:`BrokerSpec` entry here (or call :func:`register_broker` from a plugin).
Nothing above this file changes: the adapter resolves the gateway class and the
connect settings through the registry.

Connect settings are built from environment variables — credentials never live
in code or config files. For broker ``xtp`` the variables are::

    ALPHAPILOT_LIVE_XTP_ACCOUNT / _PASSWORD / _CLIENT_ID / _SOFTWARE_KEY
    ALPHAPILOT_LIVE_XTP_QUOTE_HOST / _QUOTE_PORT / _TRADE_HOST / _TRADE_PORT
    ALPHAPILOT_LIVE_XTP_QUOTE_PROTOCOL (TCP/UDP) / _LOG_LEVEL

``ALPHAPILOT_LIVE_<BROKER>_SETTING_JSON`` overrides the whole dict (raw JSON in
the gateway's native keys) for anything the field map doesn't cover.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Callable, Mapping

ENV_PREFIX = "ALPHAPILOT_LIVE_"


@dataclass(frozen=True)
class SettingField:
    """One connect-setting entry: env suffix -> the gateway's native (Chinese) key."""

    env_suffix: str
    gateway_key: str
    cast: Callable[[str], Any] = str
    default: Any = ""
    required: bool | None = None

    @property
    def is_required(self) -> bool:
        """Whether this field must be provided through env.

        Back-compat: historically an empty-string default meant required.
        ``required`` exists for non-empty defaults that are still mandatory,
        such as integer ports whose native gateway default is ``0``.
        """
        if self.required is None:
            return self.default == ""
        return self.required


# vn.py A-share stock gateways share this connect-setting shape.
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

_EMT_FIELDS: tuple[SettingField, ...] = _COMMON_FIELDS + (
    SettingField("QUOTE_ACCOUNT", "行情账号", str, "", required=False),
    SettingField("QUOTE_PASSWORD", "行情密码", str, "", required=False),
)


@dataclass(frozen=True)
class BrokerCapabilities:
    """Feature flags exposed to the live runtime/control plane.

    The gateway contract stays small, but real brokers differ in useful ways
    (market data, contract replay, margin, history). Keeping those differences
    in registry metadata lets CLI/Portal disable unsupported workflows without
    leaking SDK-specific conditionals upward.
    """

    asset_classes: tuple[str, ...] = ("stock", "fund", "bond")
    supports_tick: bool = True
    supports_contract_query: bool = True
    supports_account_query: bool = True
    supports_position_query: bool = True
    supports_order_query: bool = False
    supports_trade_query: bool = False
    supports_cancel: bool = True
    supports_margin: bool = False
    supports_history: bool = False


@dataclass(frozen=True)
class BrokerSpec:
    """Everything the live system needs to drive one broker."""

    name: str                      # registry key, lowercase (e.g. "xtp")
    gateway_path: str              # "package.module:ClassName" of the vn.py gateway
    gateway_name: str              # vn.py gateway_name passed to MainEngine calls
    setting_fields: tuple[SettingField, ...] = field(default=_COMMON_FIELDS)
    description: str = ""
    capabilities: BrokerCapabilities = field(default_factory=BrokerCapabilities)


_BROKERS: dict[str, BrokerSpec] = {}


def register_broker(spec: BrokerSpec) -> None:
    _BROKERS[spec.name.lower()] = spec


def get_broker(name: str) -> BrokerSpec:
    spec = _BROKERS.get(name.lower())
    if spec is None:
        raise ValueError(f"unknown broker {name!r}; registered: {sorted(_BROKERS)}")
    return spec


def list_brokers() -> list[BrokerSpec]:
    return [_BROKERS[k] for k in sorted(_BROKERS)]


def resolve_gateway_class(name: str) -> Any:
    """Import and return the vn.py gateway class for broker ``name``.

    Raises ImportError with an actionable message when the gateway package is
    not installed on this machine (e.g. running on macOS where the broker SDKs
    have no build).
    """
    spec = get_broker(name)
    module_path, cls_name = spec.gateway_path.split(":")
    try:
        module = import_module(module_path)
        return getattr(module, cls_name)
    except (ImportError, AttributeError) as exc:
        # AttributeError covers the dev-machine case where the vendored source
        # folder resolves as an empty namespace package (repo root on sys.path)
        # even though the compiled gateway is not installed.
        raise ImportError(
            f"broker {spec.name!r} needs the {module_path!r} package (compiled vn.py "
            f"gateway). Install it in the live environment (see Dockerfile.live): {exc}"
        ) from exc


def gateway_importable(name: str) -> bool:
    try:
        resolve_gateway_class(name)
        return True
    except Exception:  # noqa: BLE001 - availability probe
        return False


def create_gateway(name: str):
    """Instantiate the broker gateway for ``name`` — the one entry point callers use.

    Native gateways (AlphaPilot :class:`BrokerGateway` subclasses, e.g. XTP Pro /
    EMT) are constructed directly. Anything else is assumed to be a vn.py gateway
    class and gets wrapped in :class:`VnpyBrokerAdapter`, so legacy/vn.py brokers
    keep working through the same call.
    """
    from alphapilot.systems.live.gateway import BrokerGateway

    spec = get_broker(name)
    gateway_class = resolve_gateway_class(name)
    if isinstance(gateway_class, type) and issubclass(gateway_class, BrokerGateway):
        return gateway_class(spec.name)

    from alphapilot.systems.live.brokers.vnpy_adapter import VnpyBrokerAdapter

    return VnpyBrokerAdapter(spec.gateway_name, gateway_class=gateway_class)


def create_quote_gateway(name: str):
    """Instantiate a market-data provider for ``name``.

    Built-in EMT/XTP gateways currently expose quote and trade from one class, so
    this delegates to :func:`create_gateway`. Future pure quote providers can
    register their own factory behind this function without changing runtime code.
    """
    if name == "paper":
        return _NoopQuoteGateway("paper")
    return create_gateway(name)


class _NoopQuoteGateway:
    """A harmless placeholder quote provider for tests/config previews."""

    def __init__(self, name: str = "paper") -> None:
        self.name = name
        self._callback = None

    def register_callback(self, callback) -> None:  # noqa: ANN001
        self._callback = callback

    def connect(self, setting: dict) -> None:  # noqa: ARG002
        callback = self._callback
        handler = getattr(callback, "on_gateway_connected", None)
        if handler is not None:
            handler(self.name, "quote", "noop")

    def close(self) -> None:
        callback = self._callback
        handler = getattr(callback, "on_gateway_disconnected", None)
        if handler is not None:
            handler(self.name, "quote", "noop", halt=False)

    def subscribe(self, codes: list[str]) -> None:
        return None


def build_connect_setting(name: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build the gateway's native connect-setting dict from the environment.

    Precedence: ``..._SETTING_JSON`` (full override) > per-field env vars >
    field defaults.
    """
    spec = get_broker(name)
    env = os.environ if env is None else env
    prefix = f"{ENV_PREFIX}{spec.name.upper()}_"

    raw_json = env.get(f"{prefix}SETTING_JSON")
    if raw_json:
        parsed = json.loads(raw_json)
        if not isinstance(parsed, dict):
            raise ValueError(f"{prefix}SETTING_JSON must be a JSON object")
        return parsed

    setting: dict[str, Any] = {}
    for fld in spec.setting_fields:
        raw = env.get(prefix + fld.env_suffix)
        if raw is None or raw == "":
            setting[fld.gateway_key] = fld.default
        else:
            setting[fld.gateway_key] = fld.cast(raw)
    return setting


def build_quote_connect_setting(name: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Build quote-provider settings.

    For the current native EMT/XTP adapters a single connect call still logs in
    both quote and trade SDK channels, so the setting shape remains identical to
    the broker setting. The separate function is the public extension point for
    future pure quote providers.
    """
    if name == "paper":
        return {}
    return build_connect_setting(name, env=env)


def missing_setting_fields(name: str, env: Mapping[str, str] | None = None) -> list[str]:
    """Required env variable names still unset for broker ``name``."""
    spec = get_broker(name)
    env = os.environ if env is None else env
    prefix = f"{ENV_PREFIX}{spec.name.upper()}_"
    if env.get(f"{prefix}SETTING_JSON"):
        return []
    return [
        prefix + fld.env_suffix
        for fld in spec.setting_fields
        if fld.is_required and not env.get(prefix + fld.env_suffix)
    ]


def missing_quote_setting_fields(name: str, env: Mapping[str, str] | None = None) -> list[str]:
    """Required env vars for quote-provider connection."""
    if name == "paper":
        return []
    return missing_setting_fields(name, env=env)


def quote_provider_importable(name: str) -> bool:
    if name == "paper":
        return True
    return gateway_importable(name)


def list_quote_providers() -> list[BrokerSpec]:
    """Quote-provider catalog.

    The returned spec type intentionally mirrors BrokerSpec so the portal can
    render capabilities and env field names with the same table component.
    """
    return [
        BrokerSpec(
            name="paper",
            gateway_path="alphapilot.systems.live.brokers.paper:PaperBroker",
            gateway_name="PAPER",
            setting_fields=(),
            description="In-process PaperBroker quote sandbox",
            capabilities=BrokerCapabilities(asset_classes=("stock",), supports_tick=True),
        ),
        *list_brokers(),
    ]


# ---- built-in brokers ------------------------------------------------------ #
register_broker(
    BrokerSpec(
        name="xtp",
        gateway_path="alphapilot.systems.live.brokers.xtp_pro:XtpProGateway",
        gateway_name="XTP",
        setting_fields=_COMMON_FIELDS + (SettingField("SOFTWARE_KEY", "授权码", required=True),),
        description="中泰证券 XTP PRO（SDK 1.2.1，XTPX 新一代柜台）",
        capabilities=BrokerCapabilities(supports_order_query=False, supports_trade_query=True),
    )
)
register_broker(
    BrokerSpec(
        name="emt",
        gateway_path="alphapilot.systems.live.brokers.emt:EmtGateway",
        gateway_name="EMT",
        setting_fields=_EMT_FIELDS,
        description="东方财富证券 EMT（trade ~2.27 / quote ~2.19，原生网关）",
        capabilities=BrokerCapabilities(supports_order_query=True, supports_trade_query=True),
    )
)
