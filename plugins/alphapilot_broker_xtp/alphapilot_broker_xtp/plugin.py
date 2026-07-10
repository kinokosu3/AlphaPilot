"""Lightweight XTP plugin manifest; does not import the native SDK."""

from alphapilot.systems.live.plugin import (
    EndpointSpec,
    GatewayCapabilities,
    LivePluginSpec,
    ProviderSpec,
    QuoteChannelSpec,
    SettingField,
    TradeChannelSpec,
)

_BASE = (
    SettingField("ACCOUNT", "账号", required=True),
    SettingField("PASSWORD", "密码", required=True),
    SettingField("CLIENT_ID", "客户号", int, 1),
    SettingField("LOG_LEVEL", "日志级别", str, "INFO"),
)
_TRADE = _BASE + (
    SettingField("TRADE_HOST", "交易地址", required=True),
    SettingField("TRADE_PORT", "交易端口", int, 0, required=True),
    SettingField("SOFTWARE_KEY", "授权码", required=True),
)
_QUOTE = _BASE + (
    SettingField("QUOTE_HOST", "行情地址", required=True),
    SettingField("QUOTE_PORT", "行情端口", int, 0, required=True),
    SettingField("QUOTE_PROTOCOL", "行情协议", str, "TCP"),
)
_CAPS = GatewayCapabilities(
    exchanges=("SSE", "SZSE"),
    supports_order_query=False,
    supports_trade_query=True,
)


def get_plugin_spec() -> LivePluginSpec:
    return LivePluginSpec(
        plugin_id="xtp",
        description="中泰证券 XTP Pro plugin",
        providers=(
            ProviderSpec(
                name="xtp",
                factory_path="alphapilot_broker_xtp.factory:create_gateway",
                availability_path="alphapilot_broker_xtp.factory:check_available",
                gateway_name="XTP",
                description="中泰证券 XTP PRO（SDK 1.2.1，XTPX 新一代柜台）",
                shareable=True,
                trade=TradeChannelSpec(
                    setting_fields=_TRADE,
                    endpoints=(EndpointSpec("trade", "交易地址", "交易端口"),),
                    capabilities=_CAPS,
                ),
                quote=QuoteChannelSpec(
                    setting_fields=_QUOTE,
                    endpoints=(EndpointSpec("quote", "行情地址", "行情端口"),),
                    capabilities=_CAPS,
                ),
            ),
        ),
    )
