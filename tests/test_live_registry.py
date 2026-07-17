"""Broker registry: spec lookup, env->setting building, JSON override, adapter wiring."""

from __future__ import annotations

import sys
import types

import pytest

from alphapilot.systems.live.brokers import registry as reg


def test_installed_plugin_brokers_registered() -> None:
    names = [spec.name for spec in reg.list_brokers()]
    assert names == sorted(names)
    assert {"emt", "xtp"}.issubset(names)
    assert reg.get_broker("XTP").gateway_path == "alphapilot_broker_xtp.factory:create_gateway"
    assert reg.get_broker("xtp").distribution == "alphapilot-broker-xtp"
    assert reg.get_broker("emt").gateway_name == "EMT"


def test_core_without_plugins_has_no_real_brokers(monkeypatch: pytest.MonkeyPatch) -> None:
    reg.reset_plugin_registry_for_tests()
    monkeypatch.setattr(reg, "_entry_points", lambda: [])
    assert reg.list_brokers() == []
    assert [spec.name for spec in reg.list_quote_providers()] == ["paper"]


def test_unknown_broker_raises() -> None:
    with pytest.raises(ValueError, match="unknown trade broker"):
        reg.get_broker("nope")


def test_build_connect_setting_from_env() -> None:
    env = {
        "ALPHAPILOT_LIVE_XTP_ACCOUNT": "user1",
        "ALPHAPILOT_LIVE_XTP_PASSWORD": "pw",
        "ALPHAPILOT_LIVE_XTP_CLIENT_ID": "7",
        "ALPHAPILOT_LIVE_XTP_QUOTE_HOST": "119.0.0.1",
        "ALPHAPILOT_LIVE_XTP_QUOTE_PORT": "6002",
        "ALPHAPILOT_LIVE_XTP_TRADE_HOST": "119.0.0.2",
        "ALPHAPILOT_LIVE_XTP_TRADE_PORT": "6001",
        "ALPHAPILOT_LIVE_XTP_SOFTWARE_KEY": "key123",
    }
    setting = reg.build_connect_setting("xtp", env)
    assert setting["账号"] == "user1"
    assert setting["客户号"] == 7                       # cast to int
    assert setting["授权码"] == "key123"
    assert setting["日志级别"] == "INFO"
    quote = reg.build_quote_connect_setting("xtp", env)
    assert quote["行情端口"] == 6002
    assert quote["行情协议"] == "TCP"


def test_build_connect_setting_json_override() -> None:
    env = {"ALPHAPILOT_LIVE_EMT_SETTING_JSON": '{"账号": "a", "客户号": 3}'}
    assert reg.build_connect_setting("emt", env) == {"账号": "a", "客户号": 3}
    with pytest.raises(ValueError, match="JSON object"):
        reg.build_connect_setting("emt", {"ALPHAPILOT_LIVE_EMT_SETTING_JSON": "[1]"})


def test_emt_quote_credentials_are_optional_separate_fields() -> None:
    env = {
        "ALPHAPILOT_LIVE_EMT_ACCOUNT": "trade-user",
        "ALPHAPILOT_LIVE_EMT_PASSWORD": "trade-pass",
        "ALPHAPILOT_LIVE_EMT_QUOTE_ACCOUNT": "quote-user",
        "ALPHAPILOT_LIVE_EMT_QUOTE_PASSWORD": "quote-pass",
        "ALPHAPILOT_LIVE_EMT_QUOTE_HOST": "1.1.1.1",
        "ALPHAPILOT_LIVE_EMT_QUOTE_PORT": "1001",
        "ALPHAPILOT_LIVE_EMT_TRADE_HOST": "2.2.2.2",
        "ALPHAPILOT_LIVE_EMT_TRADE_PORT": "1002",
    }
    setting = reg.build_quote_connect_setting("emt", env)
    assert setting["账号"] == "trade-user"
    assert setting["密码"] == "trade-pass"
    assert setting["行情账号"] == "quote-user"
    assert setting["行情密码"] == "quote-pass"


def test_missing_setting_fields() -> None:
    missing = reg.missing_setting_fields("emt", {})
    assert "ALPHAPILOT_LIVE_EMT_ACCOUNT" in missing
    assert "ALPHAPILOT_LIVE_EMT_PASSWORD" in missing
    assert "ALPHAPILOT_LIVE_EMT_TRADE_PORT" in missing
    # ints with non-empty defaults are not "missing"
    assert "ALPHAPILOT_LIVE_EMT_CLIENT_ID" not in missing
    assert "ALPHAPILOT_LIVE_EMT_LOG_LEVEL" not in missing
    assert "ALPHAPILOT_LIVE_EMT_QUOTE_ACCOUNT" not in missing
    assert "ALPHAPILOT_LIVE_EMT_QUOTE_PASSWORD" not in missing
    assert reg.missing_setting_fields("emt", {"ALPHAPILOT_LIVE_EMT_SETTING_JSON": "{}"}) == []
    quote_missing = reg.missing_quote_setting_fields("emt", {})
    assert "ALPHAPILOT_LIVE_EMT_QUOTE_PORT" in quote_missing
    assert "ALPHAPILOT_LIVE_EMT_TRADE_PORT" not in quote_missing


def test_xtp_missing_fields_require_software_key_and_ports() -> None:
    missing = reg.missing_setting_fields(
        "xtp",
        {
            "ALPHAPILOT_LIVE_XTP_ACCOUNT": "user1",
            "ALPHAPILOT_LIVE_XTP_PASSWORD": "pw",
            "ALPHAPILOT_LIVE_XTP_QUOTE_HOST": "119.0.0.1",
            "ALPHAPILOT_LIVE_XTP_TRADE_HOST": "119.0.0.2",
        },
    )
    assert "ALPHAPILOT_LIVE_XTP_SOFTWARE_KEY" in missing
    assert "ALPHAPILOT_LIVE_XTP_TRADE_PORT" in missing
    assert "ALPHAPILOT_LIVE_XTP_CLIENT_ID" not in missing
    assert "ALPHAPILOT_LIVE_XTP_QUOTE_PORT" in reg.missing_quote_setting_fields("xtp", {})


def test_xtp_public_test_endpoint_defaults_do_not_override_env() -> None:
    from scripts.live_xtp_common import env_with_public_test_endpoints

    env = env_with_public_test_endpoints(
        {
            "ALPHAPILOT_LIVE_XTP_QUOTE_HOST": "1.2.3.4",
            "ALPHAPILOT_LIVE_XTP_TRADE_PORT": "7001",
        }
    )
    assert env["ALPHAPILOT_LIVE_XTP_QUOTE_HOST"] == "1.2.3.4"
    assert env["ALPHAPILOT_LIVE_XTP_TRADE_PORT"] == "7001"
    assert env["ALPHAPILOT_LIVE_XTP_TRADE_HOST"] == "120.27.164.69"
    assert env["ALPHAPILOT_LIVE_XTP_QUOTE_PORT"] == "6002"


def test_resolve_gateway_class_import_error_message(monkeypatch: pytest.MonkeyPatch) -> None:
    # A broker whose gateway package is absent -> actionable ImportError.
    spec = reg.BrokerSpec(name="ghost", gateway_path="ghost_pkg:GhostGateway", gateway_name="GHOST")
    monkeypatch.setitem(reg._BROKERS, "ghost", spec)
    with pytest.raises(ImportError, match="cannot load"):
        reg.resolve_gateway_class("ghost")
    assert not reg.gateway_importable("ghost")


def test_resolve_plugin_factories_and_sdk_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    import alphapilot_broker_emt.gateway as emt_gateway
    import alphapilot_broker_xtp.gateway as xtp_gateway

    assert callable(reg.resolve_gateway_class("xtp"))
    assert callable(reg.resolve_gateway_class("emt"))

    monkeypatch.setattr(xtp_gateway, "SDK_AVAILABLE", True)
    monkeypatch.setattr(emt_gateway, "SDK_AVAILABLE", True)
    assert reg.gateway_importable("xtp")
    assert reg.gateway_importable("emt")

    monkeypatch.setattr(xtp_gateway, "SDK_AVAILABLE", False)
    monkeypatch.setattr(emt_gateway, "SDK_AVAILABLE", False)
    assert not reg.gateway_importable("xtp")
    assert not reg.gateway_importable("emt")


def test_create_gateway_native_returns_broker_gateway() -> None:
    from alphapilot.systems.live.brokers.xtp_pro import XtpProGateway
    from alphapilot.systems.live.gateway import BrokerGateway

    gw = reg.create_gateway("xtp")
    assert isinstance(gw, XtpProGateway)
    assert isinstance(gw, BrokerGateway)
    assert gw.name == "xtp"
    assert gw.roles == frozenset({"trade"})
    assert gw.md_api is None


def test_plugin_pair_shares_same_provider_and_splits_mixed_channels() -> None:
    emt_trade, emt_quote = reg.create_gateway_pair("emt", "emt")
    assert emt_trade is emt_quote
    assert emt_trade.roles == frozenset({"trade", "quote"})
    assert emt_trade.td_api is not None
    assert emt_trade.md_api is not None

    mixed_trade, mixed_quote = reg.create_gateway_pair("emt", "xtp")
    assert mixed_trade is not mixed_quote
    assert mixed_trade.roles == frozenset({"trade"})
    assert mixed_trade.td_api is not None and mixed_trade.md_api is None
    assert mixed_quote.roles == frozenset({"quote"})
    assert mixed_quote.md_api is not None and mixed_quote.td_api is None


def test_create_gateway_wraps_vnpy_class(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-BrokerGateway class (vn.py style) gets wrapped in VnpyBrokerAdapter.
    fake = types.ModuleType("fake_vnpy_broker")

    class LegacyGateway:  # noqa: D401 - stub vn.py gateway class
        pass

    fake.LegacyGateway = LegacyGateway
    monkeypatch.setitem(sys.modules, "fake_vnpy_broker", fake)
    spec = reg.BrokerSpec(
        name="legacy", gateway_path="fake_vnpy_broker:LegacyGateway", gateway_name="LEGACY"
    )
    monkeypatch.setitem(reg._BROKERS, "legacy", spec)
    from alphapilot.systems.live.brokers.vnpy_adapter import VnpyBrokerAdapter

    gw = reg.create_gateway("legacy")
    assert isinstance(gw, VnpyBrokerAdapter)
    assert gw.gateway_name == "LEGACY"


def test_register_custom_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = reg.BrokerSpec(name="demo", gateway_path="demo_pkg:DemoGateway", gateway_name="DEMO")
    monkeypatch.setitem(reg._BROKERS, "demo", spec)
    assert reg.get_broker("demo").gateway_name == "DEMO"
    setting = reg.build_connect_setting("demo", {"ALPHAPILOT_LIVE_DEMO_ACCOUNT": "x"})
    assert setting["账号"] == "x"


def test_adapter_connect_uses_registry_when_setting_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from alphapilot.systems.live.brokers.vnpy_adapter import VnpyBinding, VnpyBrokerAdapter

    sent = {}

    class FakeMainEngine:
        def connect(self, setting, name):
            sent["setting"] = setting
            sent["name"] = name

    class FakeEventEngine:
        def register(self, *_a):
            pass

    binding = VnpyBinding(
        main_engine=FakeMainEngine(), event_engine=FakeEventEngine(), gateway_name="XTP",
        OrderRequestCls=None, CancelRequestCls=None, SubscribeRequestCls=None,
        Direction=None, Offset=None, OrderType=None, Exchange=None,
        EVENT_ORDER="o", EVENT_TRADE="t", EVENT_POSITION="p",
        EVENT_ACCOUNT="a", EVENT_CONTRACT="c", EVENT_TICK="k",
    )
    adapter = VnpyBrokerAdapter("XTP", binding=binding)
    monkeypatch.setenv("ALPHAPILOT_LIVE_XTP_ACCOUNT", "envuser")
    adapter.connect({})
    assert sent["name"] == "XTP"
    assert sent["setting"]["账号"] == "envuser"          # built from env via registry
