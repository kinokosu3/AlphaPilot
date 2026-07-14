# 策略到交易全链路

本文记录 AlphaPilot 当前已经落地的策略实例、统一决策、回放、PAPER、SHADOW 和受控
LIVE 链路，以及尚未满足的外部上线门槛。这里的“贯通”表示代码路径和安全控制闭环，
不表示策略具备盈利能力，也不表示未经券商 UAT 即可投入真实资金。

## 当前范围

两类策略保持并行，各自使用同一套下游组合、账户和执行基础设施：

```text
规则择时 -> TimingSignal --------------------+
                                                +-> PortfolioPolicy
Qlib/模型选股 -> CrossSectionalSignal --------+          |
                                                           v
PortfolioDecision(D 日) -> AccountSizer(D+1) -> TargetPortfolio
                                                   |
                                                   v
ExecutionPlan -> OMS -> Risk -> Broker -> fills/reconcile
```

- 规则择时用于判断单只股票、ETF、指数或市场在时间序列上的状态。
- 模型选股用于同一时点在股票池横截面上输出分数和排名。
- 两者均可独立创建实例、预览、统一回放、PAPER、SHADOW；A 股/ETF 多头日频实例可在
  通过门禁后进入 LIVE。
- 本轮没有实现“选股 + 择时”的组合算法，也不允许一个实例绑定多个信号源。
  `PortfolioInputs` 已预留并行输入契约，后续组合规则应作为独立
  `PortfolioPolicy` 实现和验证。
- 分钟级实例只允许 REPLAY、PAPER 和 SHADOW，不允许晋升 LIVE。

## 模块边界

正式链路使用以下单向依赖：

```text
trading.contracts
       ^
timing / selection / data adapters
       ^
trading.application
       |
       v (Protocol)
live / replay / persistence adapters
       |
       v
OMS -> Risk -> Broker
```

核心约束：

- `trading.contracts` 是无 live、timing、backtest 依赖的纯类型层。
- 策略 provider 只读取完整 Bar 和只读上下文，不接触 Broker，也不返回 Broker 订单。
- Qlib 推理位于 selection adapter；核心只认识 `SignalProvider` 和类型化信号。
- 组合政策只把信号转换为目标权重；账户 sizing 和订单规划位于其下游。
- 正式 daemon 只启动已经验证的 `instance_id`，不解释任意策略名。
- 旧 timing/backtest/daemon 路径仅作为兼容适配器保留，不是新实例的反向依赖。

依赖边界由测试固定，防止重新形成 `trading <-> timing/live/backtest` 循环。

## 公共契约

主要公共类型位于 `alphapilot.systems.trading.contracts`：

- `CompletedBar`：只允许已完成的、带频率和数据版本的 feature Bar。
- `TradableQuote`：只允许不复权原始价格，用于估值、sizing 和成交。
- `InstrumentMetadata`：交易单位、最小价格步长、资产类型和 T+0/T+1 元数据。
- `SignalEnvelope`：包含信号类型、来源实例、时点、有效期、数据/模型版本。
- `PortfolioDecision`：D 日信号及目标权重，绑定 D+1 生效交易日和配置哈希。
- `AccountSnapshot`、`TargetPortfolio`：不可变账户真值和目标股数。
- `FeeSchedule`：买卖费率、最低费用和单笔拆单上限；sizing 会按实际拆单数量预留最低费用。
- `ExecutionPlan`：阶段、稳定子订单引用、问题列表和恢复版本。
- `OperatorContext`：敏感操作的操作员、请求、原因和认证来源。

数据口径被强制分开：策略特征可按实例声明使用复权数据；成交、估值和目标股数只能
使用不复权可交易价格。日期、标的、数据版本、交易日历或 Bar 完成状态不一致时，实例
保持预热或阻断决策，不能静默混用数据。

## 策略定义与实例

`StrategyDefinition` 描述一类可复用策略，包括版本、信号类型、参数 JSON Schema、
所需历史长度、支持频率、可部署模式和代码哈希。`StrategyInstanceConfig` 则绑定一组
具体参数、股票池、频率、数据政策、组合政策和不可变 artifact，并生成稳定
`config_hash`。

因此简单均线策略同样采用“类与实例分离”：一个 `dual_ma` 定义可以产生
`ma_5_20`、`ma_20_60` 等实例，无需机器学习模型。任何参数、股票池、代码、模型、
数据政策或组合政策变化都会产生新配置哈希，使旧证据和 LIVE 授权失效。
活动、暂停待对账或异常状态的实例不允许直接修改，必须先通过部署协调器正式停止；修改后
实例自动回到 REPLAY，不能让旧 runner 与新配置同时存在。
对已存在实例进行这些变更时，部署级别同时重置为 REPLAY，不能沿用原有晋升级别。

注册优先级为内置策略、本地显式清单、pip entry point。重复 ID、错误 Schema、API
不兼容或导入失败的扩展会进入 quarantine，不阻止主进程发现其他策略。运行期间不热加载
策略目录。

### Provider API v1

v1 用于兼容现有 DataFrame 策略，实现 `generate_signals(...)` 即可。系统会将它包装成
`INSTRUMENT_TIMING` 生命周期 provider。示例清单：

```toml
[strategy]
api_version = 1
provider_api_version = 1
id = "my_dual_ma"
version = "0.1.0"
kind = "rule"
factory = "strategy:MyDualMA"
signal_kind = "instrument_timing"
required_history = 61
supported_assets = ["equity", "fund"]
supported_frequencies = ["day", "min"]
deployable_modes = ["replay", "paper", "shadow", "live"]
parameter_schema_json = '''
{"type":"object","properties":{"fast":{"type":"integer","minimum":2,"default":20},"slow":{"type":"integer","minimum":3,"default":60}},"additionalProperties":false}
'''
```

目录固定为：

```text
strategies/my_dual_ma/
|-- strategy.toml
`-- strategy.py
```

### Provider API v2

v2 直接实现完整生命周期：

```python
class MyProvider:
    def initialize(self, context): ...
    def warmup(self, history): ...
    def evaluate(self, context): ...  # 返回 SignalEnvelope
    def snapshot(self): ...
    def restore(self, state): ...
    def stop(self, reason): ...
```

清单将 `api_version` 和 `provider_api_version` 设置为 `2`，并明确
`signal_kind`。本地和 pip 第三方 v2 provider 在常驻独立 worker 内执行生命周期调用，
受超时和资源限制约束；worker 超时、崩溃或状态恢复失败会使实例进入 `ERROR/HALTED`。
这只是故障隔离，插件仍被视为可信 Python 代码，不是安全沙箱。

pip 包可使用 `alphapilot.strategies` entry point 返回一个或多个
`StrategyDefinition`。

## 组合政策扩展

信号到权重只有一个入口：`PortfolioPolicy.build(inputs, context)`。策略不能把分数直接
伪装成账户仓位。当前内置政策为：

- `timing_fixed_exposure`：把个股 long/flat 择时状态转为受现金缓冲和单标的上限约束的权重。
- `selection_topk_dropout_equal_weight`：按横截面分数执行 Top-K、有限换仓、现金缓冲、
  单标的上限和等权配置。

自定义政策放在 `policies/<policy_id>/policy.toml`，或通过
`alphapilot.portfolio_policies` entry point 注册。政策必须声明支持的 `SignalKind` 和参数
Schema。当前不会注册 composite policy。

## Qlib 选股实例

`qlib_selection` 是 provider v2，只输出股票池横截面分数和排名，不读取 Broker，也不会
修改研究阶段的滚动模拟账户。

自动实例必须从已保存的研究 `StrategyRecord` 创建。创建时，模型、因子定义和必要模板会被
复制到该实例专属的不可变 runtime artifact 目录；manifest 绑定研究资产名、股票池、数据
版本、模型 SHA-256、因子版本和内容指纹。Qlib 模板必须位于研究资产目录内，模板树的路径
和全部文件内容也会进入哈希。运行时会同时验证目录归属、manifest 和文件哈希，自动部署
不能传入任意 `model_pickle_path`。

旧 `model_target` 继续作为人工 UAT/恢复入口，明确标记 `origin=manual`，不会形成自动部署
证据。

## 日频决策与 D+1 执行

统一 `DecisionPipeline` 按以下顺序运行：

```text
加载实例 -> 校验 config/artifact -> 预热 -> provider.evaluate
-> policy.build -> 全组合校验 -> 保存 PortfolioDecision
-> effective_session 到达 -> 读取账户/原始行情/合约 -> sizing -> ExecutionPlan
```

D 日收盘只保存信号和目标权重。D+1 集合竞价读取新的账户、活动订单、停牌/涨跌停状态和
不复权行情，再计算目标股数；不会用 D 日收盘价直接下单，也不会在错过有效期后补发旧目标。
正式运行必须配置可用的交易所日历；真实账户模式不会退回“工作日即交易日”的估算。实例的
`data_policy.data_version` 也必须明确设置后才具备部署资格，以便所有信号、回测和证据可复现。

首次信号同样与 OMS 实际持仓比较：信号为空仓而账户仍持仓时，会产生平仓目标。目标计算
包含活动买卖单和部分成交，避免重启或重复决策造成重复补仓、超卖。

## REPLAY、PAPER、SHADOW 与 LIVE

| 模式 | 数据/账户 | 共享决策与执行状态机 | 可路由 Broker |
|---|---|---|---|
| REPLAY | 历史 Bar + 模拟账户 | 是 | 仅历史撮合器 |
| PAPER | 配置的数据源 + 模拟账户 | 是 | 仅模拟 Broker |
| SHADOW | 真实行情 + 真实账户只读快照 | 是 | 永远否 |
| LIVE | 真实行情 + 真实账户 | 是 | 通过全部授权后才是 |

`ReplayRuntime` 使用与运行时相同的 provider、policy、sizer、planner、OMS 规则和执行状态机，
只替换时钟与 Broker。历史撮合支持费用、滑点、整手、T+1、停牌、涨跌停、拒单和部分成交。
产物包含 manifest、signals、weights、targets、plans、child orders、fills、positions、equity
和 summary，并绑定实例/配置/代码/模型/数据/政策版本。

## 可恢复执行状态机

每个计划持久化为：

```text
PLANNED
-> SELLING
-> WAITING_SELL_REPORTS
-> REFRESHING_ACCOUNT
-> BUYING
-> FINAL_RECONCILE
-> COMPLETED
```

- 卖单未终结前不会生成买单；卖出回报后重新读取账户和可用资金。
- 部分成交会刷新账户和活动订单，再计算剩余数量。
- 稳定引用格式为
  `instance_id/config_hash/decision_id/instrument/side/child_index`。
- 后续补单使用持久化的下一个 `child_index`，不会复用已提交引用。
- 任意阶段重启都先查询 Broker 委托、成交、持仓和资金，再从已确认状态继续。
- 外部订单、缺失回报、撤单失败、账户差异或日志损坏会暂停并要求人工对账。
- SHADOW 走相同状态转换，但路由端口永久 `can_route=False`。

## LIVE 安全边界

首期假设使用专用自动交易账户，一个账户只允许一个 LIVE writer。发现股票池外持仓、来源
不明持仓或外部活动订单时会阻断，不会自动卖出。LIVE writer 运行期间禁止普通人工新开仓；
人工卖出/撤单、kill switch 和受审计的恢复操作保留。

自动子订单必须通过 `AutomatedRouteAuthorizer`，并匹配实例、配置哈希、账户、Broker、部署
级别、runtime、desired/observed state、心跳和对账状态。实例、账户和全局三级 kill switch
均可阻止新订单，取消委托始终允许。

LIVE 还要求：

1. 当前配置至少 20 个不同交易日 PAPER 通过证据；
2. 当前配置至少 5 个不同交易日 SHADOW 通过证据；
3. 操作员确认基准持仓；
4. 生成并消费一次性、短有效期且绑定实例/配置/账户/Broker 的 LIVE approval；
5. `ALPHAPILOT_AUTOMATED_LIVE_ENABLED=true`。默认值仍是 `false`。

LIVE 重启固定进入 `PAUSED_PENDING_RECONCILE`。对账成功后仍需操作员显式恢复，不会自动
重新路由。

## 操作员认证和审计

Portal 默认监听 `127.0.0.1`。`/api/trading` 写操作、LIVE 控制、kill switch 变更和人工
恢复要求 Bearer token；数据库只保存 token 哈希和 token ID，明文仅在生成时返回一次。
Portal 只在当前浏览器会话内存中保存 token，不写入持久化 localStorage。

为 UAT 和人工恢复保留的旧 `/api/live/*` 写接口也经过同一 Bearer token 边界并写入审计，
不能通过旧入口绕过正式部署授权；只读状态、行情查询和 preflight 探测不要求令牌。

所有敏感动作记录操作员、原因、请求 ID、实例、配置哈希、账户、Broker 和结果。CLI 的本地
操作同样写入审计事件，生命周期控制、LIVE 授权和 kill switch 变更必须提供原因。

## 正式 API 和 CLI

正式 API 位于 `/api/trading`：

- 策略与政策：`strategy-definitions`、`portfolio-policy-definitions`、
  `strategy-instances`、`strategy-instances/from-research-asset`；
- 预览与回测：`strategy-instances/{id}/preview`、
  `strategy-instances/{id}/backtest-runs`、`backtest-runs/{run_id}`；
- 部署：`deployments/{id}/promote|authorize-live|start|pause|reconcile|resume|stop|status`；
- 运行证据与控制：`deployments/{id}/stage-runs`、`kill-switches`、`audit-events`。

同步 `POST .../{id}/backtest` 和泛化 `POST .../deployments/{id}/{action}` 仅保留兼容，新增
调用应使用异步 backtest-run 和显式生命周期路由。

正式 CLI 使用 `trading_*` 前缀，覆盖定义/政策列表、实例创建、研究资产导入、校验、预览、
异步回测、晋升、操作员令牌、LIVE approval、start/pause/reconcile/resume/stop、kill switch
和审计查询。正式部署命令只接受 `instance_id`。

## 持久化和恢复

trading runtime SQLite 当前 schema 版本为 v5，使用 WAL 和顺序 migration。升级前创建在线
备份，迁移在单事务中执行；迁移失败会保留原库并阻止自动路由，不会静默重建。

数据库保存实例、artifact manifest、信号、决策、异步回测、执行阶段/尝试、子订单、成交
对账、runtime desired/observed state、stage run、路由阻断、操作员 token、LIVE approval、
基准持仓、审计事件和旧入口调用计数。稳定 decision/order 引用有唯一约束。

启动恢复顺序固定为：读取检查点和执行日志，查询 Broker 账户/持仓/委托/成交，修复本地
投影并检测差异；差异未解决时维持暂停并阻断路由。

## 兼容入口和删除条件

本轮不删除旧入口：

- `/api/timing/*` 和 `timing_*` CLI 是新能力迁移期间的兼容入口；API 返回标准弃用信息并
  记录调用量。
- `/api/live/daemon/strategy/*` 不再由 Portal 调用，但内部 daemon 控制命令仍保留。
- daemon 的 strategy-name 参数只保留 PAPER 兼容并记录调用量。
- `/api/strategies/*`、`strategy_create`、`strategy_backtest` 仍是研究资产接口，不等同于
  正式可部署实例，因此暂不删除。
- 手工 `live_order`、`live_cancel`、`live_submit_target` 是 UAT 和恢复入口，暂不删除。

某个旧公开入口只有同时满足以下条件才可在单独破坏性版本删除：

1. 正式 API/CLI 已具备功能等价能力；
2. Portal 和第一方 CLI 已零调用；
3. 遥测显示至少一个稳定发布周期且连续 30 天无外部调用；
4. 已发布 `Deprecation`、`Link`/替代入口和明确 Sunset 通知；
5. 兼容测试已转为迁移测试，并有回滚方案。

研究资产接口与人工恢复入口不能因为名称“旧”就一并删除；它们承担不同职责。

## 验证状态与上线门槛

当前代码闭环由 644 项后端测试、90 项 Portal 测试、TypeScript 类型检查、生产构建、
依赖边界、两类策略黄金链路、D/D+1、行情口径、部分成交、重启、SHADOW 禁路由、授权、
kill switch 和 SQLite 迁移测试覆盖。

仍需在目标环境完成以下外部工作，才能称为“小规模实盘试运行”：

- 当前配置真实连续运行 20 个 PAPER 交易日和 5 个 SHADOW 交易日；
- SHADOW 决策与同数据离线回放一致；
- XTP/EMT 完成账户/合约/行情查询、下单、部分成交、撤单、断线重连、进程重启恢复和
  kill switch UAT；
- 使用专用账户、受限资金、标的白名单和人工监控。

在这些证据完成前，应保持 `ALPHAPILOT_AUTOMATED_LIVE_ENABLED=false`。
