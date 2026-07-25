# 模拟与实盘交易

> **风险提示：** 默认保持 `ALPHAPILOT_AUTOMATED_LIVE_ENABLED=false`。即使部署不再要求逐级晋升，也应在确认风控、账户绑定、行情、对账和恢复流程后才启用真实路由。

## 适用场景与前置条件

用于 PAPER 演练、仿真柜台、SHADOW 观察、人工运维和受控 LIVE 部署。PAPER 只需本地环境；simulation/live 需要对应插件、受限账户、合约与行情能力。自动策略必须有已验证实例和匹配当前配置哈希的独立部署。

## 运行边界

```mermaid
flowchart LR
    C[CLI / Portal / DeploymentCoordinator] --> R[LiveRuntime]
    R --> E[LiveEngine]
    E --> O[OMS]
    O --> G[RiskGate]
    G --> B[Broker Gateway]
    B --> O
    O --> L[Ledger / Runtime State]
```

| 模式 | 数据/账户 | 可路由 | 用途 |
|---|---|---:|---|
| `dry_run` | 本地 | 否 | 只规划 |
| `paper` | 本地模拟 | 仅 PaperBroker | 功能演练 |
| `simulation` | 外部仿真柜台 | 按插件配置 | TTS 等柜台仿真 |
| `shadow` | 真实只读 | 否 | 与 LIVE-plan 一致性验证 |
| `live` | 真实 | 受授权控制 | 小规模实盘 |

![模拟与实盘页](../assets/portal/live.png)

## 最安全的开始方式

```bash
alphapilot live_status
alphapilot live_plugins
alphapilot live_preflight --broker=paper
alphapilot live_daemon_start \
  --mode=paper --symbols=600000.SSE --cash=100000
alphapilot live_daemon_status --mode=paper
```

结束时：

```bash
alphapilot live_daemon_stop --mode=paper
```

## 运行时动态行情订阅

daemon 启动后可以显式添加观察标的，不需要停止进程或重新登录券商：

```bash
# 未绑定策略的独立 daemon
alphapilot live_daemon_subscribe \
  --symbols=000001.SZ,600519.SSE --wait=True --timeout=5

# 已启动的正式策略部署
alphapilot trading_deployment_subscribe \
  --instance_id=ma_5_20 --symbols=000001.SZ
```

Portal「实盘交易」页在 daemon 运行时会单独启用“添加观察订阅”。它不会因为切换 K 线下拉框而自动订阅；正式部署运行时使用该部署的隔离接口，否则使用独立 daemon 接口。

| 字段 | 来源 | 进入策略计算 | 生命周期 |
|---|---|---:|---|
| `strategy_symbols` | 正式实例 universe | 是 | 随正式部署 daemon |
| `observer_symbols` | Portal/CLI 显式添加；独立 daemon 启动参数也归此类 | 否 | 停止 daemon 后清空 |
| `subscribed_symbols` / `symbols` | 前两者并集，供旧客户端兼容 | 不直接决定 | 随 daemon |

一次请求会返回 `requested`、`added`、`already_subscribed`、`failed` 和 `awaiting_first_tick`。其中 `added` 只表示 Provider 的同步订阅调用已接受；非交易时段、7×24 回放尚未轮到该标的或 Provider 后续异步拒绝时，仍可能没有 Tick。Portal 会保留该标的并显示“等待首个 Tick”或陈旧状态，不会把它误报成实时行情已就绪。

动态 observer 最多 50 个。请求会先整体完成代码规范化、未知交易所和 Provider 交易所能力校验；超过上限会整批拒绝。通过校验后逐标的调用 Provider，所以同步失败允许部分成功。与 strategy 重复的 observer 不会重复订阅，并保留 strategy 来源。本版本不提供取消订阅；需要清空时停止 daemon，历史 Tick/K 线文件仍会保留。

Bar 服务会对两类标的的并集生成 Tick 快照、1/5 分钟和日 K，并写入同一个行情 Provider 数据目录。策略 Runner 在 Bar listener 和消费入口各按固定 universe 过滤一次，因此 observer Bar 不会进入 `_pending_session`、因子、模型或订单决策，也不会修改部署状态、`config_hash`、`binding_hash`、stale、Kill Switch 或路由权限。显式 reconnect 会先恢复 strategy，再恢复 observer；strategy 恢复失败继续按交易安全规则阻断，observer 恢复失败只写诊断。

升级前已经在运行的旧 daemon 不认识 `subscribe_observer` 命令。Portal 遇到 `unsupported daemon command` 会提示版本不一致；升级后先重启一次 daemon，此后新增标的不再需要重启。

## 人工订单和目标组合

人工订单与自动策略授权分离，并记录 `origin=manual`：

```bash
alphapilot live_daemon_order \
  --mode=paper --symbol=600000.SSE --side=buy \
  --volume=100 --price=10.00
```

`live_submit_target`/`live_daemon_submit_target` 可以只生成计划或显式路由。涨停卖、跌停买是否被接受由合约涨跌停、价格步长、行情新鲜度和 RiskGate 决定，不受 Broker UAT 工具的 ±1% 挂单场景限制。

## Portal 操作员鉴权

Portal 默认在所有 `/api/live` 和 `/api/trading` 写操作上要求操作员令牌；命令行交易本身继续使用 `local-cli` 审计，不受这个 Portal HTTP 开关影响。查看或切换模式：

```bash
alphapilot portal_operator_auth
alphapilot portal_operator_auth \
  --required=false \
  --operator_id=alice \
  --reason="trusted lab network" \
  --acknowledge_network_risk=true
alphapilot portal_restart
```

`optional` 模式不会关闭确认、账户绑定、对账、单账户 writer lock、Kill Switch、RiskGate 或 automated LIVE 环境开关，但它会允许无令牌客户端调用策略、部署、daemon、Kill Switch 和手工订单接口。若 Portal 监听 `0.0.0.0`，通配 CORS 还允许跨站网页发起请求。启动日志、实盘/策略页面和 `/api/portal/security` 会持续显示警告；通用审计记录 request ID、路径、方法、结果、客户端地址、Origin 与 User-Agent，不记录凭据或请求载荷。

## 自动策略部署

正式部署只接受持久化 `instance_id`：

```bash
alphapilot trading_deploy --instance_id=ma_5_20 --run_mode=paper
alphapilot trading_start --instance_id=ma_5_20
alphapilot trading_status --instance_id=ma_5_20
alphapilot trading_pause --instance_id=ma_5_20
alphapilot trading_reconcile --instance_id=ma_5_20
alphapilot trading_resume --instance_id=ma_5_20
alphapilot trading_stop --instance_id=ma_5_20
```

LIVE 重启后固定进入 `PAUSED_PENDING_RECONCILE`，不会自动恢复下单。pause/stop 会尽力撤销实例活动订单并撤销路由授权。

## Kill switch

```bash
alphapilot trading_kill_switch \
  --scope_type=instance --scope_id=ma_5_20 \
  --active=True --reason="manual halt"
```

支持实例、账户和全局三级。启用后阻止新订单，但撤单始终允许。解除前必须记录操作员和原因。

## SHADOW、诊断和 UAT

- SHADOW 使用真实账户和行情生成与 LIVE 相同的目标及计划，路由端口永久关闭。
- `trading_diagnostics` 汇总各运行模式的会话和异常；`trading_decision_compare` 可比较任意两次回放/部署运行。二者都不改变部署权限。
- XTP/EMT/TTS UAT 只能由本地 `trading_broker_uat_*` CLI 发起，且受白名单和金额上限约束；它是可选验收工具，不是 LIVE 门禁，也不限制普通用户和策略委托价格。

券商安装和环境变量见 [XTP/EMT](../live-xtp.md)、[TTS](../tts-simulation.md) 和[插件开发](../live-plugins.md)。

## 输入、输出与审计产物

输入包括 workspace、Broker/行情源、账户、标的、订单或目标组合。输出包括 runtime state、不可变 ledger、OMS 委托/成交、部署配置、运行诊断、可选决策比较/UAT 和操作员审计。状态 JSON 是投影，Broker 查询与 SQLite/ledger 才共同构成恢复依据；不要单独编辑任一文件。

## Fail-closed 条件

Broker 断线、账户未同步、行情过期、合约缺失、停牌、状态损坏、未知外部订单、撤单失败或配置哈希变化都会阻断自动路由。不要通过手工改 SQLite 或状态 JSON 绕过阻断。

## 常见错误

- daemon 无法启动：先运行 preflight，核对插件、native SDK、环境字段和端口。
- 订单被拒：查看 RiskGate 原因、合约价格边界、交易单位、现金和交易时段。
- 重启后不能恢复：这是预期的 fail-closed 行为，应先 reconcile 再人工 resume。
- SHADOW 有计划但无委托：SHADOW 永久 `can_route=False`，不是故障。
- optional 模式仍返回 401：请求主动携带了无效或过期令牌；清除该令牌，或换成有效令牌以保留真实操作员身份。
- 添加观察标的后一直无行情：先确认返回中的 `failed`、`awaiting_first_tick`、交易时段和 Provider 日志；SDK 接受订阅不等于已经收到 Tick。
- 动态订阅提示版本不一致：停止并重启升级前已经运行的 daemon 一次。
