# 自定义策略、PortfolioPolicy 与 artifact

## 扩展边界

```mermaid
flowchart LR
    Provider[SignalProvider] --> Envelope[SignalEnvelope]
    Envelope --> Inputs[PortfolioInputs]
    Inputs --> Policy[PortfolioPolicy]
    Policy --> Weights[TargetWeights]
    Weights --> Sizer[AccountSizer]
    Sizer --> Plan[ExecutionPlan]
    Plan --> Route[受控执行端口]
```

策略只负责信号，PortfolioPolicy 只负责目标权重。二者都不允许访问 Broker、提交订单或修改账户状态。

## Provider v1

v1 适配已有 DataFrame 策略：

```python
class MyTimingStrategy:
    required_history = 21

    def generate_signals(self, frame):
        result = frame.copy()
        result["signal"] = (result["close"] > result["close"].rolling(20).mean()).astype(int)
        return result
```

系统把它包装为 `INSTRUMENT_TIMING` provider。v1 用于低成本兼容，不支持精细生命周期状态；它不会被 v2 自动删除。

## Provider v2

v2 实现 `initialize`、`warmup`、`evaluate`、`snapshot`、`restore` 和 `stop`，并返回 `SignalEnvelope`。`evaluate` 必须是确定性的：相同 `as_of`、历史哈希和前置状态只能产生同一输出；历史修订应 fail closed。

定义需要声明：`strategy_id`、版本、API 版本、`signal_kind`、参数 JSON Schema、资产/频率、`required_history`、可部署模式、状态版本和代码哈希。

## 本地 manifest

```toml
strategy_id = "my_timing"
version = "1.0.0"
provider_api_version = "v2"
import_path = "strategy:MyProvider"
signal_kind = "instrument_timing"
required_history = 21
```

放置在 `strategies/<strategy_id>/strategy.toml`。只在进程启动或没有活动 LIVE 实例时刷新；冲突或导入失败进入隔离，不应覆盖内置定义。

pip 包使用 `alphapilot.strategies`；政策包使用 `alphapilot.portfolio_policies`。第三方 worker 有超时和资源限制，但仍按可信代码处理。

## PortfolioPolicy

政策实现 `build(inputs, context) -> TargetWeights`，必须声明支持的 `SignalKind`、版本和参数 Schema。不得在 policy 内做股数取整、费用或 Broker 状态处理；这些属于 AccountSizer 和 planner。

## Artifact 与变更

模型选股实例只使用研究资产快照。manifest 绑定模型 SHA-256、因子版本、股票池、数据和政策版本。任何绑定变化都重算 `config_hash`，撤销授权并使旧 stage/parity 证据失效。

测试至少覆盖 v1/v2、manifest、entry point、状态恢复、历史确定性、worker 超时、非法参数、重复 ID 和 replay/paper/shadow 信号一致性。
