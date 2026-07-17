# AlphaPilot 实盘插件开发与安装

AlphaPilot 在进程启动时扫描 `alphapilot.live.plugins` entry point。插件安装、
卸载后需要重启 Portal 和 live daemon；已安装 provider 之间切换只需停止并重启
daemon。Portal 不执行 pip 命令。

## 安装和卸载

推荐发布并使用包含 SDK wheel、公共适配器 wheel 和 broker 插件 wheel 的私有索引
或本地 wheelhouse。pip 会自动安装依赖：

```bash
pip install alphapilot-broker-xtp --find-links /path/to/wheelhouse
alphapilot live_plugins
alphapilot live_brokers
alphapilot live_quote_providers

pip uninstall alphapilot-broker-xtp
```

TTS 柜台仿真和 XTPX/EMT 一样维护在独立仓库中；一个 wheel 同时包含原生绑定和
热插拔适配器：

```bash
pip install "git+https://github.com/ai-yang/alphapilot_tts.git"
```

`tts` 是 `account_kind=simulation` 的 trade-only provider；`tts_7x24` 是
`data_kind=replay` 的 quote-only provider。完整配置和 UAT 见
[OpenCTP TTS 柜台仿真接入](tts-simulation.md)。

卸载适配插件后，即使 `alphapilot_xtpx` SDK wheel 仍留在环境中，XTP 也不会被发现。
SDK 绑定包只提供原生模块，不注册 entry point；`alphapilot-broker-*` 才是
AlphaPilot 可发现、可卸载的通道插件。TTS 是例外：`alphapilot-tts` 将原生模块和
entry point 放在同一个平台 wheel 中，卸载该 wheel 会同时移除两者。

从旧命名迁移时，先移除原来的绑定包，再安装 broker 插件；不要让两套 SDK
命名空间长期并存：

```bash
pip uninstall vnpy_xtp vnpy_emt
pip install alphapilot-broker-xtp alphapilot-broker-emt --find-links /path/to/wheelhouse
```

仓库内开发安装也保持同一层次：

```bash
pip install ./alphapilot_xtpx
pip install ./plugins/alphapilot_broker_xcommon
pip install ./plugins/alphapilot_broker_xtp
pip install ./alphapilot_tts
```

## 最小 manifest

插件入口必须轻量，不能在目录发现阶段导入原生 SDK：

```toml
[project.entry-points."alphapilot.live.plugins"]
demo = "alphapilot_broker_demo.plugin:get_plugin_spec"
```

```python
from alphapilot.systems.live import (
    LivePluginSpec,
    ProviderSpec,
    QuoteChannelSpec,
    TradeChannelSpec,
)

def get_plugin_spec():
    return LivePluginSpec(
        plugin_id="demo",
        providers=(ProviderSpec(
            name="demo",
            gateway_name="DEMO",
            factory_path="alphapilot_broker_demo.factory:create_gateway",
            trade=TradeChannelSpec(),       # 纯行情插件可省略
            quote=QuoteChannelSpec(),       # 纯交易插件可省略
            shareable=True,
        ),),
    )
```

factory 接收 `name` 和 `roles`。`roles={"trade", "quote"}` 时返回对象必须同时满足
`BrokerGateway` 和 `QuoteGateway`；纯交易或纯行情 provider 只需满足对应协议。SDK
导入应放在 factory 调用内部，并通过 `availability_path` 提供无连接可用性检查。

连接字段由 `TradeChannelSpec`、`QuoteChannelSpec` 分别声明。核心负责从环境变量构建
设置、报告缺失项和执行 TCP 预检，目录和 API 永远只输出变量名，不输出值。

trade channel 应声明 `account_kind=live|simulation|local`，quote channel 应声明
`data_kind=realtime|replay|synthetic`。`GatewayCapabilities` 同时区分 SDK 原生支持的
`native_asset_classes` 与 AlphaPilot 当前允许送单的 `routable_asset_classes`，并声明
session、position、offset 和 notional 模型。目录能力不能绕过运行环境或 RiskGate。

## 约束

- `plugin_id` 必须等于 entry point 名称。
- provider 名称必须为小写字母、数字、点、下划线或连字符，`paper` 为保留名。
- 同一角色出现重复 provider 名称时，所有冲突实现都会被禁用。
- 插件 API 版本必须等于核心的 `PLUGIN_API_VERSION`。
- 插件可以声明 futures 能力，但当前 live 风控仍拒绝真实期货路由。
- pip 插件是可信代码，与 AlphaPilot 进程拥有相同系统权限。
