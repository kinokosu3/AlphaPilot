# 策略实例、注册与实盘部署

AlphaPilot 将“策略定义”和“策略实例”分开：`dual_ma` 是策略定义，`ma_5_20`
和 `ma_20_60` 是两份参数、标的池和配置哈希不同的实例。策略只生成信号或账户目标，
不能直接访问 Broker；所有订单统一经过账户 sizing、执行计划、OMS 和风控。

## 本地策略

本地策略必须使用显式清单，系统不会递归导入任意 `.py` 文件：

```text
strategies/my_strategy/
├── strategy.py
└── strategy.toml
```

```toml
[strategy]
api_version = 1
id = "my_strategy"
version = "0.1.0"
kind = "rule"
factory = "strategy:MyStrategy"
required_history = 21
supported_assets = ["equity", "fund"]
supported_frequencies = ["day", "min"]
parameter_schema_json = '''
{"type":"object","properties":{"window":{"type":"integer","minimum":2,"default":20}},"additionalProperties":false}
'''
```

兼容现有批量策略的最小实现仍是：

```python
class MyStrategy:
    def __init__(self, window=20):
        self.window = window

    def generate_signals(self, bars, context):
        # 返回 datetime/instrument/signal/target_percent/score/reason
        ...
```

第三方批量策略在有超时和内存限制的子进程中计算，用于隔离崩溃与死循环；插件仍属于
可信 Python 代码，并不是安全沙箱。实盘运行期间禁止刷新或热加载策略目录。

## pip 插件

包可以注册 `alphapilot.strategies` entry point。入口返回一个或多个
`StrategyDefinition`，其中 factory 应保持惰性导入。内置策略优先于本地清单，本地清单
优先于 pip；重复 ID、API 版本不兼容或导入失败的定义会进入 quarantine。

## 生命周期与部署

实例状态为 `CREATED → VALIDATED → WARMING_UP → READY`，运行时进入
`RUNNING/PAUSED/HALTED/ERROR/STOPPED`。部署必须按 `REPLAY → PAPER → SHADOW → LIVE`
逐级晋升，每一级都需要由运行流程写入通过证据。

LIVE 授权绑定账户、Broker、实例 ID 和配置哈希。参数、模型、代码或标的池变化会使旧授权
失效；同一个账户只允许一个 LIVE 目标写入者。重启后的 LIVE runner 固定进入
`PAUSED_PENDING_RECONCILE`，完成 Broker/OMS 对账并由操作员恢复后才继续。

主要接口：

- `GET /api/trading/strategy-definitions`
- `GET|POST|PATCH /api/trading/strategy-instances`
- `POST /api/trading/strategy-instances/{id}/validate`
- `POST /api/trading/strategy-instances/{id}/backtest`
- `GET /api/trading/deployments/{id}`
- `POST /api/trading/deployments/{id}/promote`
- `POST /api/trading/deployments/{id}/pause|resume|stop`

旧 `/api/timing/*` 和 daemon strategy-name 调用仍可用于研究与 PAPER，响应会给出新接口的
迁移提示；LIVE 自动策略只接受已经晋升的 `instance_id`。
