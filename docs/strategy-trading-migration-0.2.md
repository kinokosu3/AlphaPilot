# 0.2.0 策略链路迁移、券商 UAT 与旧入口删除手册

本文是 AlphaPilot 从 0.1.x 兼容入口迁移到正式 `/api/trading`、`trading_*` 链路的操作手册。
代码具备这些能力不等于删除门禁已经通过。旧入口删除使用一次性完整等价验收、正式入口零调用
证明和真实 XTP/EMT UAT v2；自动策略晋升 LIVE 仍独立要求 20 个 PAPER 交易日、5 个 SHADOW
交易日和逐日 parity。两类证据都不能由单元测试或人工填写代替。

## 已实现的链路

规则择时和 Qlib 横截面选股分别通过同一条下游链路运行：

```text
provider -> SignalEnvelope -> PortfolioPolicy -> PortfolioDecision
         -> D+1 AccountSizer -> ExecutionPlan -> OMS -> Risk -> Broker
```

`data_policy.history_window` 是实例配置哈希的一部分。provider 第一次创建时才执行
`initialize + warmup`；checkpoint 恢复只执行 `restore`。同一模式下，同一 `as_of` 的历史哈希
发生变化会 fail closed。每个决策保存历史、provider 前后状态、信号、权重、政策、代码、模型
和数据版本哈希。日线 Bar 统一按交易日期标识，避免历史回放的 `00:00` 和实盘收盘时间造成
伪差异。

旧 timing signal/backtest 目前由 `LegacyTimingCompatibilityAdapter` 转成仅允许 REPLAY 的临时
实例，再调用正式 registry、decision pipeline、policy、ReplayRuntime 和执行状态机。临时实例
不能晋升，也不能形成 PAPER、SHADOW 或自动路由证据。已完成的旧 Portal timing job 会幂等导入
`backtest_runs`，并从正式 detail API 只读访问原 CSV 产物。

## 正式替代入口

| 0.1.x 兼容入口 | 正式入口 |
|---|---|
| `/api/timing/strategies`、`timing_strategies` | `/api/trading/strategy-definitions`、`trading_definitions` |
| `/api/timing/signal`、`timing_signal` | 创建实例后调用实例 `/preview`、`trading_preview` |
| `/api/timing/backtest`、`timing_backtest` | 实例异步 `/backtest-runs`、`trading_backtest --wait` |
| `/api/timing/jobs/{id}/detail` | `/api/trading/backtest-runs/{run_id}/detail` |
| `/api/jobs` 的 `timing_backtest` kind | 实例异步 `/backtest-runs` |
| `/api/modules/run` 的 timing 兼容命令 | 正式 `/api/trading` 资源接口 |
| `/api/live/daemon/strategy/*` | `/api/trading/deployments/{id}/{start,pause,reconcile,resume,stop,status}` |
| `live_daemon_strategy_*` CLI | `trading_{status,start,pause,reconcile,resume,stop}` |
| daemon strategy-name 参数 | `strategy_instance_id` |
| 同步实例 backtest | 异步 backtest run |
| 泛化 deployment action | 五个显式生命周期路由 |
| 手工 stage start/finish/evaluate | runtime 自动 stage run + 只读 qualification |

兼容 HTTP 响应固定返回 `Deprecation: true`、发布清单中的 `Sunset` 和 successor `Link`；14 个
旧 HTTP 操作也在 OpenAPI 中标记 `deprecated: true`。8 个旧 CLI 的 help 和执行输出均显示 successor。
`/api/trading/compatibility` 暴露 23 个兼容面的机器可读等价矩阵，包含输入/输出语义、状态副作用、
权限边界、测试 ID 和最终处置。调用事件记录真实入口、环境、客户端类型/版本、来源和请求 ID 哈希；
通用 job、module dispatcher 与 CLI 分开计数，避免一次请求污染多个零调用指标。Sunset 不能通过环境
变量修改。

## REPLAY 与 SHADOW 一致性

每次 REPLAY、PAPER、SHADOW 和 LIVE-plan 都保存 `DecisionObservation`。正式比较命令为：

```bash
alphapilot trading_parity_start INSTANCE_ID REPLAY_RUN_ID SHADOW_STAGE_RUN_ID
alphapilot trading_parity_status PARITY_RUN_ID
alphapilot trading_qualification INSTANCE_ID
```

日频按交易日比较。相同历史、provider 前置状态、数据、模型和政策版本时，信号或权重不同为
`MISMATCH`；输入不同、任一侧缺失或同一交易日存在歧义观测为 `NOT_COMPARABLE`；只有确定性输出
一致才是 `PASS`。账户、原始报价和合约哈希也相同时才额外比较目标股数与执行计划。

`live_qualification` 从 SQLite 运行事实派生，不接收调用方填写的交易日数。当前配置必须同时满足：

- PAPER 至少 20 个不同交易日且无未解决异常、重复路由、仓位越界或对账警告；
- SHADOW 至少 5 个不同交易日，全部交易日都有唯一 parity PASS；
- 当前账户/Broker 的 UAT 证据有效；
- 无待对账状态，实例仍是当前配置哈希并处于 SHADOW。

## XTP/EMT 真实 UAT

UAT 只能从本机 CLI 启动、恢复或终止。Portal 和 API 只有只读查询接口。先在私有 secret 文件或
进程环境中配置券商凭据；不得把密码、software key 或 token 放入 CLI 参数、数据库或产物。

仓库提供的本地包装器只读取已知 XTP/EMT 字段，并要求 `.env`、`secrets.txt` 权限为 `0600`。
先执行不会下单的预检：

```bash
python scripts/broker_uat_local.py preflight \
  --broker=xtp --secret-file=.env --symbols=510300.SSE --max-notional=20000
python scripts/broker_uat_local.py preflight \
  --broker=emt --secret-file=secrets.txt --symbols=510500.SSE --max-notional=20000
```

包装器在子进程内设置以下安全变量；不得把密码、software key 或 token 放入命令行、数据库或
产物：

```dotenv
ALPHAPILOT_BROKER_UAT_ENABLED=true
ALPHAPILOT_BROKER_UAT_ENVIRONMENT=xtp-test-account-a
ALPHAPILOT_BROKER_UAT_WHITELIST=600000.SSE
ALPHAPILOT_BROKER_UAT_MAX_NOTIONAL=20000
```

一次运行示例（价格和数量必须来自刚完成的预检；两笔子订单累计不得超过 20,000 元）：

```bash
python scripts/broker_uat_local.py start \
  --broker=xtp --secret-file=.env --symbol=510300.SSE --side=buy \
  --volume=<at-least-two-lots> --price=<fresh-marketable-limit> --max-notional=20000 \
  --confirmation=I_UNDERSTAND_REAL_ORDERS
# start 返回 restart_required 后，必须从一个新启动的本地进程恢复：
python scripts/broker_uat_local.py resume \
  --broker=xtp --secret-file=.env --run-id=<RUN_ID> \
  --confirmation=I_UNDERSTAND_REAL_ORDERS
```

失败后的再次恢复和受审计终止：

```bash
alphapilot trading_broker_uat_resume <RUN_ID> I_UNDERSTAND_REAL_ORDERS
alphapilot trading_broker_uat_abort <RUN_ID> I_UNDERSTAND_REAL_ORDERS \
  "operator diagnosed and cancelled remaining order"
```

Harness 先验证 trade/quote 插件可导入、SDK/进程架构、必需凭据字段及声明的网络端点，主动订阅
白名单标的并等待新鲜行情。UAT v2 使用同一 OMS、Risk 和 Broker callback 提交两笔子订单：第一笔
可成交限价单必须有真实成交回报，第二笔挂单保持活动，从而稳定验证计划层“已成交+剩余”、
撤余单、断线重连、进程重启恢复、稳定引用和三级 kill switch。`start` 在余单仍活动时写入
`restart_required` 检查点并关闭当前 runtime；`resume` 会拒绝原进程，只有新的本地 CLI 进程才能
重新查询委托并继续。此检查点期间必须由操作员持续监控，并立即执行 `resume` 或 `abort`。
唯一真实委托引用在 SQLite 中原子
认领；即使在券商受理与 callback 落库之间崩溃，也不会自动重发。Kill switch 探针没有可路由引用，
取消委托始终允许。每个 route claim 都在 SQLite 内原子累计请求名义金额，失败重试也不能突破
20,000 元上限。证据绑定 Git commit、实盘核心代码、Broker、账户哈希、环境、native SDK、插件和
gateway SHA-256，逐次 callback 状态、请求金额与成交金额进入 schema v8，90 天后过期；任何核心
artifact 变化立即失效。UAT 日志和产物必须运行 `scripts/check_secret_leaks.py`，输出只包含命中数，
不显示匹配内容。

每次 UAT 后应立即将 `ALPHAPILOT_BROKER_UAT_ENABLED` 恢复为 `false`。普通 CI 使用模拟 callback，
不会访问真实账户，也不能生成可用于晋升的真实环境证据。

## 多环境零调用证明

每个受控环境使用稳定的 `ALPHAPILOT_ENVIRONMENT_ID`。第一方客户端全部迁移后，在每个环境开始
完整验收周期：

```bash
alphapilot trading_compatibility --set-cutoff=true
```

任一旧调用都会增加该环境的 post-cutoff 计数；需要修复调用方并重新设置 cutoff，再完整执行一轮
仅使用正式新入口的 API/CLI/daemon/Portal/SHADOW 验收和两家券商 UAT。周期结束后，各环境导出
哈希报告：

```bash
alphapilot trading_compatibility \
  --export-path=reports/compatibility-env-a.json
```

将受控环境报告复制到发布检查环境并从本地 CLI 导入：

```bash
alphapilot trading_compatibility \
  --import-path=reports/compatibility-env-a.json
```

报告绑定环境 ID、schema、cutoff、代码 commit、逐入口计数、活动 legacy runtime 数、未导入旧 job
数和 SHA-256。导入报告哈希或总数不一致
会被拒绝。删除检查以所有环境中最晚的 cutoff 为验收起点；更早的接口验收与 UAT 证据不会复用，
并且每份环境报告都必须在整个验收周期完成后重新导出。假如存在无法观测或无法
提交最终报告的第三方客户端，删除门禁视为不满足。

## 数据库迁移与回滚

runtime SQLite 从 v5 顺序迁移到 v6、v7、v8。v6 增加确定性历史/checkpoint、provenance、详细兼容
调用、观测和 parity；v7 增加券商 UAT、唯一 UAT route claim、旧 job 映射和多环境报告；v8 增加
UAT v2、核心 artifact 指纹、累计请求/成交金额和 callback 状态序列。

迁移前使用 SQLite online backup，全部 DDL 和 config rehash 位于一个 `BEGIN IMMEDIATE` 事务。
存在活动 runtime 时 v6 拒绝迁移。history window 或 timing policy 归属变化会重算配置哈希，撤销
旧 stage evidence、approval 和自动路由绑定，并把实例退回 REPLAY。失败时原 v5 数据库及备份保留，
自动路由保持阻断。恢复时先正式 stop runtime、保留失败库、从 `backup-v5-*` 复制到新的恢复路径，
诊断后重新迁移；不得删除原库或静默创建空库。

## 0.2.0 删除门禁

先运行只读检查：

```bash
alphapilot trading_removal_check ACCEPTANCE_INSTANCE_ID
```

`removal_qualification` 必须同时通过 timing 等价矩阵、第一方生产源码除兼容层外零旧引用、全部
环境完整且零调用、XTP 与 EMT 当前核心代码/插件/native SDK UAT、无活动 legacy runner/job、活动
UAT 委托、未导入旧 job 或对账差异，以及干净 Git commit。`live_qualification` 中的 20/5 日和
parity 会并列展示，但不阻止旧入口删除。全量发布验证由固定脚本产生，不能填写布尔值：

```bash
python -m pip install -e '.[test]'
python scripts/verify_trading_removal.py --build-kind=compatibility
```

脚本拒绝 dirty worktree，并实际运行全量离线 pytest、Portal coverage/typecheck/build、OpenAPI、CLI、
依赖边界、相对固定 0.1.x 基线的 Python/TypeScript 变更行 90% coverage，以及 wheel 安装/import
smoke。`reports/trading-compatibility-verification.json` 绑定待发布兼容 commit 和报告哈希；缺失、
失败、过期 commit 或篡改都会使 removal check 失败。删除提交完成后再运行
`--build-kind=removal`，保留独立的 `reports/trading-removal-verification.json`，并额外验证旧 HTTP、
CLI、daemon 参数和 Portal 调用均已消失。

只有最终报告 `ready=true` 时，才在单独的 0.2.0 破坏性提交删除兼容入口。研究资产
`/api/strategies/*`、`strategy_create/strategy_backtest`，以及人工恢复用 `live_order`、
`live_cancel`、`live_submit_target` 不属于删除范围。

当前仓库实现了门禁和演练工具；只有兼容构建报告、两家真实模拟 UAT、cutoff 后正式入口验收与
零调用证明全部通过，才能执行 0.2.0 删除提交。自动 LIVE 默认仍为关闭状态，且仍受独立 20/5 日
门禁约束。
