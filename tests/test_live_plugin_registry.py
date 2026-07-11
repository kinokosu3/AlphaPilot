"""Versioned live plugin discovery, isolation and role-aware construction."""

from __future__ import annotations

import sys
import types

from alphapilot.systems.live.brokers import registry as reg
from alphapilot.systems.live.gateway import BrokerGateway
from alphapilot.systems.live.plugin import (
    GatewayCapabilities,
    LivePluginSpec,
    ProviderSpec,
    QuoteChannelSpec,
    SettingField,
    TradeChannelSpec,
)
from alphapilot.systems.live.types import CancelRequest, OrderRequest


class FakeDist:
    def __init__(self, name: str, version: str = "1.0") -> None:
        self.metadata = {"Name": name}
        self.version = version


class FakeEntryPoint:
    def __init__(self, name: str, loaded, *, error: Exception | None = None) -> None:  # noqa: ANN001
        self.name = name
        self._loaded = loaded
        self._error = error
        self.dist = FakeDist(f"demo-{name}")

    def load(self):
        if self._error is not None:
            raise self._error
        return self._loaded


def _plugin(
    plugin_id: str = "demo",
    provider: str = "demo",
    *,
    factory_path: str = "not_imported_native:create",
    api_version: int = 1,
) -> LivePluginSpec:
    caps = GatewayCapabilities(asset_classes=("stock",))
    return LivePluginSpec(
        plugin_id=plugin_id,
        api_version=api_version,
        providers=(
            ProviderSpec(
                name=provider,
                factory_path=factory_path,
                gateway_name=provider.upper(),
                trade=TradeChannelSpec(
                    setting_fields=(SettingField("ACCOUNT", "account", required=True),),
                    capabilities=caps,
                ),
                quote=QuoteChannelSpec(capabilities=caps),
                shareable=True,
            ),
        ),
    )


def test_discovery_is_lazy_and_records_distribution(monkeypatch) -> None:
    reg.reset_plugin_registry_for_tests()
    sys.modules.pop("not_imported_native", None)
    monkeypatch.setattr(reg, "_entry_points", lambda: [FakeEntryPoint("demo", lambda: _plugin())])

    assert [item.name for item in reg.list_brokers()] == ["demo"]
    assert "not_imported_native" not in sys.modules
    assert reg.get_broker("demo").distribution == "demo-demo"
    assert reg.plugin_diagnostics()["plugins"][0]["status"] == "loaded"


def test_broken_and_incompatible_plugins_do_not_hide_valid_one(monkeypatch) -> None:
    reg.reset_plugin_registry_for_tests()
    monkeypatch.setattr(
        reg,
        "_entry_points",
        lambda: [
            FakeEntryPoint("broken", None, error=RuntimeError("boom")),
            FakeEntryPoint("future", lambda: _plugin("future", "future", api_version=99)),
            FakeEntryPoint("demo", lambda: _plugin()),
        ],
    )

    assert [item.name for item in reg.list_brokers()] == ["demo"]
    diagnostics = reg.plugin_diagnostics()
    assert {item["plugin_id"] for item in diagnostics["issues"]} == {"broken", "future"}
    assert all("boom" not in row.get("description", "") for row in diagnostics["plugins"])


def test_duplicate_provider_is_removed_instead_of_last_wins(monkeypatch) -> None:
    reg.reset_plugin_registry_for_tests()
    monkeypatch.setattr(
        reg,
        "_entry_points",
        lambda: [
            FakeEntryPoint("one", lambda: _plugin("one", "shared")),
            FakeEntryPoint("two", lambda: _plugin("two", "shared")),
        ],
    )

    assert reg.list_brokers() == []
    assert [item.name for item in reg.list_quote_providers()] == ["paper"]
    issues = reg.plugin_diagnostics()["issues"]
    assert len([item for item in issues if item["kind"] == "duplicate_provider"]) == 4


class RoleGateway(BrokerGateway):
    created: list["RoleGateway"] = []

    def __init__(self, name: str, roles: frozenset[str]) -> None:
        super().__init__(name)
        self.roles = roles
        self.connected = 0
        self.closed = 0
        type(self).created.append(self)

    def connect(self, setting: dict) -> None:  # noqa: ARG002
        self.connected += 1

    def close(self) -> None:
        self.closed += 1

    def send_order(self, req: OrderRequest) -> str:  # noqa: ARG002
        return "role-1"

    def cancel_order(self, req: CancelRequest) -> None:  # noqa: ARG002
        return None

    def query_account(self) -> None:
        return None

    def query_position(self) -> None:
        return None

    def subscribe(self, codes: list[str]) -> None:  # noqa: ARG002
        return None


def test_shareable_provider_factory_is_called_once_for_both_roles(monkeypatch) -> None:
    module = types.ModuleType("role_plugin_factory")
    module.create = lambda *, name, roles: RoleGateway(name, roles)
    monkeypatch.setitem(sys.modules, "role_plugin_factory", module)
    reg.reset_plugin_registry_for_tests()
    monkeypatch.setattr(
        reg,
        "_entry_points",
        lambda: [FakeEntryPoint("demo", lambda: _plugin(factory_path="role_plugin_factory:create"))],
    )
    RoleGateway.created.clear()

    trade, quote = reg.create_gateway_pair("demo", "demo")
    assert trade is quote
    assert trade.roles == frozenset({"trade", "quote"})
    assert RoleGateway.created == [trade]


def test_channel_json_override_precedes_legacy_json(monkeypatch) -> None:
    reg.reset_plugin_registry_for_tests()
    monkeypatch.setattr(reg, "_entry_points", lambda: [FakeEntryPoint("demo", lambda: _plugin())])
    env = {
        "ALPHAPILOT_LIVE_DEMO_SETTING_JSON": '{"source": "legacy"}',
        "ALPHAPILOT_LIVE_DEMO_TRADE_SETTING_JSON": '{"source": "trade"}',
        "ALPHAPILOT_LIVE_DEMO_QUOTE_SETTING_JSON": '{"source": "quote"}',
    }
    assert reg.build_connect_setting("demo", env) == {"source": "trade"}
    assert reg.build_quote_connect_setting("demo", env) == {"source": "quote"}
