# OpenCTP TTS 柜台仿真接入

AlphaPilot 把 TTS 作为外部、有服务端资金/持仓/委托/成交状态的柜台仿真环境，而不是
本地 mock。TTS 交易通道不提供可用于自动交易的实盘行情，因此交易与行情 provider
始终独立选择。官方说明见 [OpenCTP TTS-CTPAPI](https://github.com/openctp/openctp/tree/master/TTS-CTPAPI)。

## 运行边界

| 维度 | 当前实现 |
| --- | --- |
| 策略晋级级别 | 仍为 `REPLAY → PAPER → SHADOW → LIVE`；不新增级别 |
| PAPER 执行环境 | `local_paper` 或 `broker_simulation`，二者的证据不能混用 |
| TTS trade provider | `tts`，只在 `simulation` 中可选 |
| 行情 | 任意已安装的 `realtime` provider；也可选 `tts_7x24` 回放 |
| 自动路由 | 仅 `simulation + realtime quote + 完整对账 + 单写者锁` |
| 7x24 回放 | 只允许观察、录制和 Dry-run；手工及策略路由均强制拒绝 |
| 可路由品种 | SSE/SZSE 股票、基金 |
| 明确拒绝 | 债券、期权、期货、未知合约、非实时行情 |

PAPER 晋级 SHADOW 时会在同一事务内切换为 Live 配置中的交易与行情 provider；
TTS 的 binding hash 不会被带入 SHADOW。已在晋级事务中验证并消费的 PAPER 证据仍可
用于后续资格审计，但不能被新 binding 当作本阶段证据。SHADOW 晋级 LIVE 默认沿用该
阶段已经验证过的独立行情 provider。

这次接入复用现有 `DecisionPipeline → ExecutionPlanner → ExecutionCoordinator →
LiveEngine → RiskGate → BrokerGateway` 边界。没有修改策略信号、组合决策、A 股执行
计划、OMS 或持仓计算算法；新增的是部署绑定、授权、环境隔离和 TTS gateway。

## 包与热插拔

实现位于独立仓库 [ai-yang/alphapilot_tts](https://github.com/ai-yang/alphapilot_tts)，
一个 `alphapilot-tts` 发行包同时安装两个 Python 模块：

- `alphapilot_ttsapi`：Meson/pybind11 原生绑定和 TTS 6.3.15 SDK；不依赖 vn.py。
- `alphapilot_broker_tts`：通过 `alphapilot.live.plugins` entry point 注册的适配器。

克隆独立仓库并安装：

```bash
git clone https://github.com/ai-yang/alphapilot_tts.git
python -m pip install ./alphapilot_tts

alphapilot live_plugins
alphapilot live_brokers
alphapilot live_quote_providers
```

安装或卸载插件后要重启 Portal 和 daemon；插件目录发现不会导入原生 SDK。卸载
`alphapilot-tts` 后 provider 会从下次进程启动的目录中消失。

SDK 包包含 Linux x86_64、Windows x64、macOS arm64 的头文件和库，不包含 Win32。
来源、版本和逐文件 SHA256 在独立仓库的 `SDK_MANIFEST.json`，第三方声明在
`THIRD_PARTY_NOTICES.md`。Linux wheel 已接入 `Dockerfile.live` 的 `live-tts` 阶段；
Windows/macOS wheel 要在对应原生机器构建和导入验证。

## 凭据与行情配置

TTS trade-only provider 从环境读取：

```bash
export ALPHAPILOT_LIVE_TTS_USER_ID='<user>'
export ALPHAPILOT_LIVE_TTS_PASSWORD='<password>'
# OpenCTP 免费仿真通常为空；私有环境有值时再填写。
export ALPHAPILOT_LIVE_TTS_BROKER_ID=''
export ALPHAPILOT_LIVE_TTS_TRADE_FRONT='tcp://host:port'
export ALPHAPILOT_LIVE_TTS_APP_ID='<optional-app-id>'
export ALPHAPILOT_LIVE_TTS_AUTH_CODE='<optional-auth-code>'
# 只有已确认产品规则的基金才能显式加入；逗号分隔。默认全部按 T+1。
export ALPHAPILOT_LIVE_TTS_T0_FUNDS=''
```

实时行情继续使用已安装 XTP/EMT provider 的 `ALPHAPILOT_LIVE_XTP_*` 或
`ALPHAPILOT_LIVE_EMT_*` 变量。TTS 7x24 回放行情使用：

```bash
export ALPHAPILOT_LIVE_TTS_7X24_USER_ID='<user>'
export ALPHAPILOT_LIVE_TTS_7X24_PASSWORD='<password>'
export ALPHAPILOT_LIVE_TTS_7X24_BROKER_ID=''
export ALPHAPILOT_LIVE_TTS_7X24_QUOTE_FRONT='tcp://host:port'
```

凭据不会写入配置、SQLite、状态快照、ledger 或 UAT 证据。外部账户状态只输出
`sha256` 哈希；账户级 kill switch 也接受该哈希。

## 策略绑定与运行

先按正常流程把策略晋级到 PAPER，并确保 daemon 已停止。然后绑定物理执行环境：

```bash
alphapilot trading_bind_execution \
  --instance_id my_strategy \
  --execution_environment broker_simulation \
  --trade_provider tts \
  --quote_provider xtp \
  --account_profile tts-main

alphapilot trading_execution_binding --instance_id my_strategy
alphapilot trading_start --instance_id my_strategy --reason 'start TTS simulation'
```

也可以在 Portal「实盘交易 → 柜台仿真」中选择 PAPER 策略、TTS 交易方、行情源和
账户配置别名。绑定只在 daemon 停止时可变更；变更会生成新 `binding_hash`，使原
PAPER 环境的阶段证据失效。

正式策略运行时使用：

```text
runtimes/<execution_environment>/<trade>--<quote>/<binding_hash-prefix>/
```

Portal/CLI 直接启动、未绑定策略的运维 daemon 使用相同前缀下的 `standalone/`。
无选择参数的旧 CLI 仍读取原目录，以保持兼容；要操作并行 daemon，命令中需带上
`--mode`、`--trade_broker` 和 `--quote_provider`。

state、ledger 和行情快照分别在各自根目录下采用同一命名空间，中央策略部署 SQLite
保持共享。因此实盘、TTS 和本地 Paper 可以并行，但同一外部账户配置只允许一个活动
自动策略写入者。

TTS 启动顺序是认证（如需要）、登录、结算确认、合约查询、资金、持仓、当日委托和
当日成交查询。交易私有流和公共流使用 CTP `RESTART` 模式，从当日开始重放；查询经过
流控队列，成交按稳定键去重，委托重放不会让已终态订单倒退为活动状态。恢复时还会比较
同一 binding 上次快照的账户哈希和总持仓（忽略正常隔夜结算引起的今昨仓拆分变化），
并核对 ledger 与柜台委托/成交。只有所有快照完成且恢复没有差异时才允许
`reconcile → resume`；任何查询错误、断线、未知活动委托/成交或对账 warning 都会保持
暂停。

## A 股字段边界

- 柜台合约决定 `price_tick` 和最小委托数量。
- AlphaPilot 股票/基金订单保持 `Offset.NONE`；SDK 边界将买入映射为 OPEN、卖出映射
  为 CLOSE，订单和成交回报再归一化为 `NONE`。
- 股票和基金默认 T+1；只有 `T0_FUNDS` 中显式确认的基金使用 T+0 可卖数量。
- 原生 SDK 可发现期货、期权和债券合约，但 `asset_routing` 和 TTS gateway 都会在送单
  前 fail-closed。

## 真实 TTS UAT

UAT 工具只从环境读取凭据，命令行没有账号、密码或前置地址参数。TTS 必须指定一个
`realtime` 行情 provider；`tts_7x24` 会在预检阶段被拒绝。

```bash
# 只登录并查询合约、资金、持仓、委托、成交和实时行情，不下单
python /path/to/alphapilot_tts/scripts/tts_uat_local.py preflight \
  --quote-provider xtp --symbols 600000.SSE --timeout 30

# 至少两个交易 lot：一个可成交子单 + 一个挂单/恢复/撤单子单
python /path/to/alphapilot_tts/scripts/tts_uat_local.py start \
  --quote-provider xtp --symbol 600000.SSE \
  --side buy --volume 200 --price '<跨过当前卖一且不超过涨停的限价>' \
  --max-notional 20000 \
  --confirmation I_UNDERSTAND_REAL_ORDERS

# start 会停在必须跨进程恢复的检查点；用全新 CLI 进程恢复
python /path/to/alphapilot_tts/scripts/tts_uat_local.py resume \
  --quote-provider xtp --run-id '<run-id>' \
  --confirmation I_UNDERSTAND_REAL_ORDERS
```

该流程验证柜台回报驱动的可成交委托、成交、剩余挂单、稳定 reference 去重、进程重启
后的委托恢复、账户/实例/全局 kill switch、撤单、断线重连和最终无 warning 对账。

官方没有给出免费账户的持仓重置周期。完成买入 UAT、保留非零持仓后，在第一个交易日
记录基线，并至少到下一个交易日再验证：

```bash
python /path/to/alphapilot_tts/scripts/tts_uat_local.py overnight-baseline --quote-provider xtp
# 下一个交易日，用同一 state-dir 和账号执行：
python /path/to/alphapilot_tts/scripts/tts_uat_local.py overnight-verify --quote-provider xtp
```

验证文件只含账户哈希和仓位数量。若日期、账户哈希或仓位不一致会明确失败，由此记录
服务端的实际重置行为；在完成这项 UAT 前，不应声称 TTS 隔夜持仓一定保留。

## 期货后续接入评估

目前方便扩展的部分包括交易所枚举、`Direction/Offset`、合约乘数/保证金字段、插件
原生能力声明、trade/quote 分离和 `AssetRouteProfile`。但可发现不等于可路由：当前
持仓簿、账户 sizing、RiskGate 交易时段和执行计划仍是 A 股长仓/T+1/现金名义金额模型。

正式接期货至少需要新增净仓/双向仓位模型、开平/平今优先级、保证金与强平风险、夜盘
交易日归属、期货涨跌停/最小变动价位、合约换月及对应回放/恢复测试。这属于中大型核心
改造，不能只打开 TTS 的期货枚举。本版本用显式 capability 暴露未来接口，同时在所有
实际送单入口拒绝期货。
