# 策略实例、注册与实盘部署

本文重点说明注册、部署协调和安全闭环。完整的信号、组合、回放、D+1 执行状态机、正式
API/CLI 和兼容迁移说明见[《策略到交易全链路》](strategy-trading-full-chain.md)。

AlphaPilot 将“策略定义”和“策略实例”分开：`dual_ma` 是策略定义，`ma_5_20`
和 `ma_20_60` 是两份参数、标的池和配置哈希不同的实例。策略只生成信号或账户目标，
不能直接访问 Broker；所有订单统一经过账户 sizing、执行计划、OMS 和风控。
预热长度按实例的实际窗口参数解析，例如 `dual_ma(20, 60)` 需要 61 根历史 Bar，
不会继续沿用定义默认参数 `dual_ma(5, 20)` 的 21 根。

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
`PAUSED_PENDING_RECONCILE`。对账成功只会转为 `PAUSED`，仍需操作员显式 `resume`；
Broker 外部订单、账本缺失订单、活动订单或刷新失败都会保持阻断。

部署状态由 `DeploymentCoordinator` 管理。数据库同时保存 desired state 和 daemon 确认的
observed state，以及 runtime ID、runner heartbeat、最后命令、错误和
`reconcile_required`。API 不会再通过单独更新实例生命周期来假装 daemon 已经执行命令；
命令超时、daemon 死亡或撤单未确认都会 fail closed。

## 自动路由与 SHADOW

自动子订单必须在发送前通过 `AutomatedRouteAuthorizer`，并同时匹配：

- `instance_id`、`config_hash` 和稳定子订单引用；
- `account_id`、Broker、deployment level 和 runtime ID；
- desired/observed lifecycle 均为 `RUNNING`；
- runner heartbeat 未过期且不需要恢复对账；
- 实例、账户和全局 kill switch 均未启用。

手工订单使用独立的 `origin=manual` 入口；策略 runner 使用注入的自动路由端口，不能再直接
调用 `LiveEngine.submit`。取消委托不受 route block 限制。

日频 runner 只保存信号日产生的 intent，并在真实交易日历确定的下一交易日集合竞价重新读取
账户与行情后生成订单；如果恢复或暂停跨过了该有效交易日，旧目标会被审计并丢弃，不会在更晚
日期补发。

`SHADOW` 与 `LIVE` 都连接配置的真实账户和行情，并执行相同的账户、合约、交易日历和行情
新鲜度检查；区别是 SHADOW 的 run-mode 状态机始终 `can_route=False`。因此它可以保存决策和
执行计划，但即使上层误传 `route=True` 也不会调用 Broker 下单。

三级 kill switch 接口：

- `GET /api/trading/kill-switches`
- `POST /api/trading/kill-switches/{global|account|instance}/{id}/engage`
- `POST /api/trading/kill-switches/{global|account|instance}/{id}/release`

## PAPER/SHADOW 证据与数据库迁移

PAPER/SHADOW 启动时创建绑定当前 `config_hash` 的 stage run。runner 按真实会话日期记录交易日，
并累计决策、执行计划、拒单、重复引用、仓位越界、未处理错误和恢复差异。停止后 stage run
才会完成；PAPER 默认至少 20 个交易日，SHADOW 默认至少 5 个交易日。调用方声明的天数不会
替代运行时实际记录的会话日期；同一配置多次重启或多段运行中的同一交易日只计一次。

证据接口：

- `POST /api/trading/stage-runs/{instance_id}/{paper|shadow}/start`
- `POST /api/trading/stage-runs/{run_id}/finish`
- `POST /api/trading/stage-runs/{instance_id}/{paper|shadow}/evaluate`

策略运行 SQLite 当前使用顺序 schema migration（schema v5）和 WAL。旧无版本数据库先生成
SQLite 在线备份，再在一个 `BEGIN IMMEDIATE` 事务内迁移；迁移失败会保留旧库并阻止交易系统
启动，不会删除或静默重建数据库。LIVE 单账户单写者由部分唯一索引在数据库层保证。

## 选股与择时（各自已贯通，组合仅预留）

公共 `trading.contracts` 已提供 `CROSS_SECTIONAL_SELECTION`、
`INSTRUMENT_TIMING`、`MARKET_TIMING` 三类 `SignalEnvelope`，以及可并行承载三类输入的
`PortfolioInputs` 和 `PortfolioPolicy` Protocol。这一层没有 `live` 依赖，也没有预设相乘、
过滤、再归一化或牛熊切换算法。

规则择时实例与 `qlib_selection` 选股实例目前可以各自创建、预览、统一回放和按门禁晋升，
但当前不会注册 composite 策略，也不能创建、回测或晋升组合实例；现有 `SignalRecord` 和
`OrderIntent` 继续作为兼容契约。组合政策要等选股与择时算法研究完成后单独实现和验证。

主要接口：

- `GET /api/trading/strategy-definitions`
- `GET /api/trading/portfolio-policy-definitions`
- `GET|POST|PATCH /api/trading/strategy-instances`
- `POST /api/trading/strategy-instances/{id}/validate`
- `POST /api/trading/strategy-instances/{id}/preview`
- `POST /api/trading/strategy-instances/{id}/backtest-runs`
- `GET /api/trading/deployments/{id}`
- `POST /api/trading/deployments/{id}/promote`
- `POST /api/trading/deployments/{id}/start|pause|reconcile|resume|stop|status`

旧 `/api/timing/*` 和 daemon strategy-name 调用仍可用于研究与 PAPER，响应会给出新接口的
迁移提示。旧 PAPER daemon 会在自身 `state_dir` 中创建受授权器约束的临时实例，而不会退回
不受控的 `LiveEngine.submit`；正式实例禁止跨部署状态目录运行。LIVE 自动策略只接受已经
晋升的 `instance_id`。
