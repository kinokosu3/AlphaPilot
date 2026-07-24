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
- 两者均可独立创建实例、预览、统一回放，并直接配置 PAPER、SIMULATION、SHADOW 或 LIVE；
  A 股/ETF 多头日频实例在通过运行时安全检查后可以路由 LIVE。
- 本轮没有实现“选股 + 择时”的组合算法，也不允许一个实例绑定多个信号源。
  `PortfolioInputs` 已预留并行输入契约，后续组合规则应作为独立
  `PortfolioPolicy` 实现和验证。
- 分钟级实例可回放并部署到 PAPER、SIMULATION 或 SHADOW，当前不能配置 LIVE。

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
- `DecisionProvenance`：绑定精确历史窗口、历史哈希、provider 前后状态哈希、信号/权重哈希，
  以及策略代码、模型、数据和政策版本。
- `AccountSnapshot`、`TargetPortfolio`：不可变账户真值和目标股数。
- `FeeSchedule`：买卖费率、最低费用和单笔拆单上限；sizing 会按实际拆单数量预留最低费用。
- `ExecutionPlan`：阶段、稳定子订单引用、问题列表和恢复版本。
- `OperatorContext`：敏感操作的操作员、请求、原因和认证来源。

数据口径被强制分开：策略特征可按实例声明使用复权数据；成交、估值和目标股数只能
使用不复权可交易价格。日期、标的、数据版本、交易日历或 Bar 完成状态不一致时，实例
保持预热或阻断决策，不能静默混用数据。

`data_policy.history_window` 是不可变实例配置的一部分。每个标的只向 provider 传入最后
`history_window` 根完整 Bar；标的数量、最新时间和非空数据版本必须一致。provider 仅在首次创建
时执行 `initialize + warmup`，checkpoint 恢复只执行 `restore`。同一 `as_of` 和历史哈希的重放
直接复用已保存决策；同一 `as_of` 出现不同历史哈希时 fail closed，不能覆盖原决策。

## 策略定义与实例

`StrategyDefinition` 描述一类可复用策略，包括版本、信号类型、参数 JSON Schema、
所需历史长度、支持频率、可部署模式和代码哈希。`StrategyInstanceConfig` 则绑定一组
具体参数、股票池、频率、数据政策、组合政策和不可变 artifact，并生成稳定
`config_hash`。

因此简单均线策略同样采用“类与实例分离”：一个 `dual_ma` 定义可以产生
`ma_5_20`、`ma_20_60` 等实例，无需机器学习模型。任何参数、股票池、代码、模型、
数据政策或组合政策变化都会产生新配置哈希，使已有部署标记为 `stale`。
活动、暂停待对账或异常状态的实例不允许直接修改，必须先通过部署协调器正式停止；修改后
实例自动回到待验证状态，不能让旧 runner 与新配置同时存在。原部署配置和诊断仍保留；
重新验证并重新 PUT 部署后才会绑定新 `config_hash`。

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
supported_run_modes = ["paper", "simulation", "shadow", "live"]
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

## REPLAY 操作与独立部署模式

| 模式 | 数据/账户 | 共享决策与执行状态机 | 可路由 Broker |
|---|---|---|---|
| REPLAY | 历史 Bar + 模拟账户 | 是 | 仅历史撮合器 |
| PAPER | 配置的数据源 + 模拟账户 | 是 | 仅模拟 Broker |
| SIMULATION | 外部仿真柜台 + 配置行情 | 是 | 仿真交易 Provider |
| SHADOW | 真实行情 + 真实账户只读快照 | 是 | 永远否 |
| LIVE | 真实行情 + 真实账户 | 是 | 通过运行时安全检查后才是 |

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

自动子订单必须通过 `AutomatedRouteAuthorizer`，并匹配实例、配置哈希、账户、Provider、
run mode、binding hash、runtime、desired/observed state、心跳和对账状态。实例、账户和全局三级 kill switch
均可阻止新订单，取消委托始终允许。

LIVE 不要求 PAPER/SHADOW 时长、决策比较、Broker UAT、短期 approval 或人工账户 baseline。
它仍要求 `ALPHAPILOT_AUTOMATED_LIVE_ENABLED=true`，以及当前账户/Provider/binding、单账户单
写者、合约/实时行情、对账、心跳、Kill Switch 和逐单 RiskGate 全部通过。

LIVE 重启固定进入 `PAUSED_PENDING_RECONCILE`。对账成功后仍需操作员显式恢复，不会自动
重新路由。

## 操作员认证和审计

Portal 默认监听 `127.0.0.1`，并以 `required` 模式统一保护全部 `/api/live` 与 `/api/trading`
写操作，包括部署配置和生命周期。数据库只保存 token 哈希和 token ID，明文仅在生成时返回
一次；Portal 只在当前浏览器会话内存中保存 token，不写入持久化 localStorage。

本机 `portal_operator_auth` CLI 可把模式切换为高风险 `optional`。此时无令牌请求使用
`portal-unauthenticated` 上下文，主动提供的 token 仍严格校验。非 loopback 部署与 automated
LIVE 不再被 Portal 阻断，通配 CORS 也保留，因此 `0.0.0.0 + optional` 会把交易写能力暴露给
所有可达客户端和跨站网页。只读状态、行情查询和 preflight 探测始终不要求令牌。

每次交易写请求记录 requested/outcome transport 审计，并保留现有领域审计。事件记录操作员、
原因、请求 ID、路径、方法、结果、客户端地址、Origin、User-Agent，以及适用的实例、配置哈希、
账户和 Broker；不记录凭据或请求载荷。CLI 的本地生命周期操作仍使用 `local-cli` 审计。

## 正式 API 和 CLI

正式 API 位于 `/api/trading`：

- 策略与政策：`strategy-definitions`、`portfolio-policy-definitions`、
  `strategy-instances`、`strategy-instances/from-research-asset`；
- 预览与回测：`strategy-instances/{id}/preview`、
  `strategy-instances/{id}/backtest-runs`、`backtest-runs/{run_id}`；
- 部署：`GET deployments`、`GET|PUT deployments/{id}` 和
  `deployments/{id}/start|pause|reconcile|resume|stop|status`；
- 运行诊断与控制：`deployments/{id}/diagnostics`、`kill-switches`、`audit-events`；
- 通用决策比较：`deployments/{id}/decision-comparisons`、
  `decision-comparisons/{comparison_id}`；
- 迁移与券商 UAT：`compatibility`、只读 `broker-uat-runs`。

同步 `POST .../{id}/backtest` 和泛化 `POST .../deployments/{id}/{action}` 已删除。调用方必须
使用异步 backtest-run 和显式生命周期路由，不提供兼容分发。

正式 CLI 使用 `trading_*` 前缀，覆盖定义/政策列表、实例创建、研究资产导入、校验、预览、
异步回测、独立部署、运行诊断、通用决策比较、操作员令牌、生命周期、kill switch 和审计
查询。正式部署命令接受实例、run mode、Provider 和账户绑定。

## 持久化和恢复

trading runtime SQLite 当前 schema 版本为 v10，使用 WAL。v1-v9 数据库会在任何写入前被
只读拒绝；用户必须配置新的 state/runtime store 路径，系统不会自动迁移或修改旧文件。

数据库保存实例、独立部署配置、artifact manifest、信号、决策、异步回测、执行阶段/尝试、
子订单、成交对账、runtime desired/observed state、运行诊断、决策观测/比较、路由阻断、
操作员 token、Broker UAT、审计事件和逐环境旧入口调用证据。稳定 decision/order
引用以及 UAT 的真实委托引用有唯一约束。

启动恢复顺序固定为：读取检查点和执行日志，查询 Broker 账户/持仓/委托/成交，修复本地
投影并检测差异；差异未解决时维持暂停并阻断路由。

## 0.2.0 入口收敛结果

0.2.0 在完成等价矩阵、受控环境零调用、XTP/EMT UAT 和发布构建门禁后，移除了：

- `/api/timing/*`、`timing_*` CLI 和 `timing_backtest` Portal job kind；
- `/api/live/daemon/strategy/*`、`live_daemon_strategy_*` 以及 daemon 的匿名 strategy-name 参数；
- 同步实例 backtest、泛化 deployment action 和手工 stage-run 写接口。

内部 daemon IPC 与 `strategy_instance_id` 子进程协议继续由 `RuntimeControlPort` 使用，不是公共
兼容入口。`/api/strategies/*`、`strategy_create`、`strategy_backtest` 仍用于研究资产；人工
`live_order`、`live_cancel`、`live_submit_target` 仍用于 UAT 和受审计恢复。

删除前使用的门禁为：

1. 每个旧用例通过新链路一次性完整语义等价矩阵，Portal、CLI、后台 job 和生产源码无旧引用；
2. 所有受控环境从 migration cutoff 起，在仅使用正式新入口的验收周期内旧调用为零；
3. XTP 和 EMT 的真实 UAT v2 证据均有效，且没有 legacy runner/job、活动 UAT 委托、未导入
   历史结果或未解决对账差异；
4. 固定发布脚本在待发布的干净 commit 上通过后端、Portal、OpenAPI、CLI、依赖边界、
   变更行覆盖率和 wheel smoke，并生成不可篡改的 release report；
5. `trading_removal_check` 的 commit、schema、UAT 和核心代码证据哈希与待发布代码完全一致。

旧入口删除门禁属于 0.2.0 的历史发布流程；当前研究 campaign 如需 20 个 PAPER 日、5 个
SHADOW 日、一致性或 UAT，会自行读取中性诊断并执行阈值，不约束 LIVE。详细演练与删除
步骤见[《0.2.0 策略链路迁移、券商 UAT 与旧入口删除手册》](strategy-trading-migration-0.2.md)。

研究资产接口与人工恢复入口不能因为名称“旧”就一并删除；它们承担不同职责。

## 验证状态与上线门槛

代码闭环由全量后端测试、Portal coverage/typecheck/build、OpenAPI/CLI 快照、依赖边界、
两类策略黄金链路、D/D+1、行情口径、部分成交、重启、SHADOW 禁路由、账户绑定、kill switch、
schema v10 空库创建、v1–v9 只读拒绝和 wheel smoke 共同验证。最终结果必须由固定发布脚本生成并绑定精确
commit，文档中的历史测试数量不能替代发布报告。

研究团队可以为某个 campaign 额外采用以下保守验收政策：

- 当前配置真实连续运行 20 个 PAPER 交易日和 5 个 SHADOW 交易日；
- SHADOW 决策与同数据离线回放一致；
- XTP/EMT 完成账户/合约/行情查询、下单、部分成交、撤单、断线重连、进程重启恢复和
  kill switch UAT；
- 使用专用账户、受限资金、标的白名单和人工监控。

这些证据属于 campaign 决策，不是系统 LIVE 权限门禁。无论是否采用，正式投入资金前都应先在
目标环境验证账户、合约、行情、对账、恢复与 Kill Switch；未准备承担真实风险时应保持
`ALPHAPILOT_AUTOMATED_LIVE_ENABLED=false`。
