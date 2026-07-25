# XTP Pro / EMT 实盘接入（原生网关，无 vn.py）

AlphaPilot 的实盘栈已完全去除 vn.py 依赖，并通过 Python entry point 热插拔：

- **XTP Pro (XTPX 1.2.1)**：`alphapilot-broker-xtp` 插件中的原生网关，
  底层只用编译好的 `alphapilot_xtpx.api` pybind 绑定（该子包不依赖 vn.py）。
- **EMT（东方财富）**：`alphapilot-broker-emt` 插件中的原生网关，底层
  `alphapilot_emt.api`。
- 两家共享 `alphapilot-broker-xcommon`（映射表 + 转换器 + 回调状态机）与
  AlphaPilot 核心的 `SdkBrokerGateway`（分发线程、轮询、SDK 日志目录）。
  **接入新券商 = 子类化 SdkBrokerGateway + 写映射表/转换回调**，OMS、风控、
  执行器、引擎全部复用。

线程模型：SDK 的 C++ 回调线程一进来就把处理体投递到网关的
`EventDispatcher`（单分发线程），OMS 及以上保持无锁单写者约束；资金/持仓的
2 秒轮询也在同一分发线程上执行。

恢复模型：普通轮询只查资金/持仓，避免压到券商查询限频；连接 recovery 和保守
重连会额外尝试当日委托/成交查询，并把 `requested/unsupported/errors` 写入
recovery 报告。EMT 当前支持委托和成交查询；XTP Pro 当前 pybind 绑定暴露成交查询，
但未暴露委托查询请求方法，因此委托查询会降级为 `unsupported` 并写 warning。

连接状态也通过同一分发线程进入 AlphaPilot：XTP/EMT 的交易通道断线会写入
`gateway_disconnected` 和 `disconnected` 事件，并立即触发 `LiveEngine` 的
halt，必须重连、recovery、人工检查后再恢复；行情通道断线会写入
`gateway_disconnected`，但默认不 halt，避免短暂行情重登误停交易。

## 运行环境

XTP Pro / EMT 的 Linux SDK 都是 `x86_64` 动态库（`libxtpxquoteapi.so` /
`libxtpxtraderapi.so` / `libemt_*.so`），必须在 `linux/amd64` 上运行。

Docker 路径（镜像不再包含 vn.py，只有编译工具链 + numpy/pandas + 两套绑定）：

```bash
docker compose --profile live build live
```

本机路径（x86_64 Linux，已在 conda env `alphapilot-smoke` 验证）：

```bash
conda activate alphapilot-smoke   # python 3.11 + conda-forge cxx-compiler + meson/pybind11
pip install --no-build-isolation ./alphapilot_xtpx   # XTP Pro 绑定
pip install --no-build-isolation ./alphapilot_emt   # EMT 绑定
pip install ./plugins/alphapilot_broker_xcommon
pip install ./plugins/alphapilot_broker_xtp
pip install ./plugins/alphapilot_broker_emt
python scripts/live_smoke_import.py           # 构建后冒烟
```

## 环境变量

真实凭证只放在私有 `.env` 或 `docker compose run -e ...` 参数里，不要提交到仓库。

```bash
ALPHAPILOT_LIVE_MODE=live
ALPHAPILOT_LIVE_BROKER=xtp            # 或 emt
ALPHAPILOT_LIVE_XTP_ACCOUNT=<your_xtp_account>
ALPHAPILOT_LIVE_XTP_PASSWORD=<your_xtp_password>
ALPHAPILOT_LIVE_XTP_CLIENT_ID=1
ALPHAPILOT_LIVE_XTP_SOFTWARE_KEY=<your_xtp_software_key>
ALPHAPILOT_LIVE_XTP_QUOTE_HOST=<quote_host_from_broker>
ALPHAPILOT_LIVE_XTP_QUOTE_PORT=<quote_port_from_broker>
ALPHAPILOT_LIVE_XTP_TRADE_HOST=<trade_host_from_broker>
ALPHAPILOT_LIVE_XTP_TRADE_PORT=<trade_port_from_broker>
ALPHAPILOT_LIVE_XTP_QUOTE_PROTOCOL=TCP
ALPHAPILOT_LIVE_XTP_LOG_LEVEL=INFO
```

EMT 使用同样的键名模式（`ALPHAPILOT_LIVE_EMT_*`，无 `SOFTWARE_KEY`）。
`ALPHAPILOT_LIVE_<BROKER>_SETTING_JSON` 可整体覆盖；交易和行情也可分别使用
`_TRADE_SETTING_JSON`、`_QUOTE_SETTING_JSON`，通道配置优先。SDK 自身日志目录由
`ALPHAPILOT_SDK_LOG_DIR` 控制（默认 `~/.alphapilot/sdk_logs/<broker>`）。
XTP/EMT 同一账号同一 `CLIENT_ID` 只能保持一个交易会话；如果交易登录失败但行情可用，
先尝试换一个未占用的 `ALPHAPILOT_LIVE_XTP_CLIENT_ID`（普通用户通常 1-24）。

## 1. 预检

预检不登录、不发送账号密码、不下单，只检查环境变量、SDK 架构、原生网关 +
编译绑定的可用性和 TCP 端点连通性。

```bash
python scripts/live_preflight_xtp.py --timeout 5
```

## 2. 查询型 smoke

默认只做：交易和行情登录、等待账户快照、等待合约加载、订阅一个标的并等待
tick。默认不下单。

```bash
python scripts/live_smoke_connect_xtp.py --symbol 600000 --timeout 30 --dump-logs
# 只验证交易登录和资金/持仓查询：加 --skip-tick
```

## 3. 小单下单/撤单 smoke

显式加 `--order` 才会下单：1 手、约低于最新价 10% 的限价买单，等回报后撤单。
这个脚本直接驱动 native XTP gateway，适合排查 SDK 和柜台连通性；它不经过
AlphaPilot 的 `LiveEngine/RiskGate/daemon` 控制面。

```bash
python scripts/live_smoke_connect_xtp.py --symbol 600000 --timeout 30 --tick-timeout 10 --order
```

更完整的 AlphaPilot 实盘闭环验收请使用 daemon smoke。它会启动长驻 runtime，
等待账户和 tick，经过 `live_daemon_order -> LiveEngine.submit -> RiskGate ->
BrokerGateway` 提交一笔 1 手限价买单，等待 OMS 中出现券商 ack，再通过
`live_daemon_cancel` 等待撤单确认，最后停止 daemon。

```bash
# 若默认 CLIENT_ID 被占用，可临时覆盖为未占用值，例如 2
python scripts/live_smoke_daemon_xtp.py \
  --symbol 512880 \
  --client-id 2 \
  --confirm-live \
  --wait-continuous \
  --timeout 40 \
  --event-timeout 15 \
  --dump-status
```

daemon smoke 默认价格约为 tick 参考价的 96%，是为了通过当前默认 5% 价格保护，
同时尽量避免立即成交。若希望指定价格，可传 `--price <limit>`；若真实行情跳动导致
委托成交，脚本会失败并保留 state/ledger 路径供排查。由于 daemon 路径会经过
`RiskGate`，真实下单必须在 AlphaPilot 认为可报单的交易时段内执行；夜间或收盘后
会返回类似 `session: submission not allowed in post_close` 的拒单原因。
从盘前启动时建议加 `--wait-continuous`，脚本会等到 09:30/13:00 连续竞价窗口再启动
daemon 并提交测试委托。若账户持仓/权益较小导致普通股票 1 手触发
`max_position_pct`，优先换成低名义金额 ETF（例如 `512880`），不要为了 smoke 随意放宽
实盘风控。

## 4. AlphaPilot live runtime / CLI

脚本 smoke 只用于连通性验证；正式接入 AlphaPilot 时走 `alphapilot live_*`
命令，它们复用 `LiveRuntime -> LiveEngine -> RiskGate -> BrokerGateway`，不会绕过
风控或审计 ledger。

```bash
# 查看 broker 注册、SDK 可导入性、必填 env；加 --network=True 才做 TCP 端点探测
alphapilot live_preflight --broker xtp --network=False

# 连接一次、等待账户/合约 ready、写 runtime_state.json 后退出
ALPHAPILOT_LIVE_MODE=live ALPHAPILOT_LIVE_BROKER=xtp \
alphapilot live_connect --timeout 30

# 前台运行一个最小 live runtime（不自动下单），周期写 git_ignore_folder/live_state/runtime_state.json
ALPHAPILOT_LIVE_MODE=live ALPHAPILOT_LIVE_BROKER=xtp \
alphapilot live_run --symbols 600000,000001 --interval 2

# 后台 daemon：启动 / 查看 / 停止一个长驻 LiveRuntime（不自动下单，可接收显式命令）
ALPHAPILOT_LIVE_MODE=live ALPHAPILOT_LIVE_BROKER=xtp \
alphapilot live_daemon_start --symbols 600000,000001 --interval 2
alphapilot live_daemon_subscribe --symbols 600519.SSE,510300.SSE --wait True
alphapilot live_daemon_status
alphapilot live_daemon_halt --reason manual --wait True
alphapilot live_daemon_resume --wait True
alphapilot live_daemon_refresh --wait True
alphapilot live_daemon_reconnect --wait True       # 保守重连：默认保持 halted
alphapilot live_daemon_order --symbol SH600000 --side buy --volume 100 --price 10 --confirm_live True --wait True --event_timeout 10
alphapilot live_daemon_cancel --order_id <order_id> --wait True --event_timeout 10
alphapilot live_daemon_stop

# 自动策略先创建并验证持久化实例，再独立配置 deployment
alphapilot trading_instance_create \
  --instance_id=sma20-paper --strategy_id=sma_filter --universe=SH600000 \
  --params='{"window":20}' --frequency=day \
  --data_policy='{"feature_adjustment":"backward","history_window":21,"data_version":"daily-bars-2026-07"}' \
  --portfolio_policy='{"policy_id":"timing_fixed_exposure","params":{"target_percent":0.5}}'
alphapilot trading_instance_validate --instance_id=sma20-paper
alphapilot trading_backtest --instance_id=sma20-paper \
  --options='{"data_dir":"./data","adjust_mode":"none"}' --wait=True
alphapilot trading_deploy --instance_id=sma20-paper --run_mode=paper
alphapilot trading_start --instance_id=sma20-paper
alphapilot trading_deployment_subscribe \
  --instance_id=sma20-paper --symbols=600519.SSE
alphapilot trading_status --instance_id=sma20-paper
alphapilot trading_pause --instance_id=sma20-paper
alphapilot trading_reconcile --instance_id=sma20-paper
alphapilot trading_resume --instance_id=sma20-paper
alphapilot trading_stop --instance_id=sma20-paper

# 只生成目标组合执行计划（默认不路由）
alphapilot live_submit_target \
  --holdings '{"SH600000": 1000}' \
  --prices '{"SH600000": 10.0}' \
  --mode dry_run

# paper 演练：实际走 LiveEngine.submit，但落到 PaperBroker
alphapilot live_submit_target \
  --holdings '{"SH600000": 1000}' \
  --prices '{"SH600000": 10.0}' \
  --mode paper --cash 100000 --route True

# 真实路由必须显式确认；目标可来自 target JSON、inline holdings，或 daily-trade session
ALPHAPILOT_LIVE_MODE=live ALPHAPILOT_LIVE_BROKER=xtp \
alphapilot live_submit_target --session demo_session --route True --confirm_live True

# 长驻 daemon 内路由目标组合；返回 planned/submitted/unrouted/fully_routed
alphapilot live_daemon_submit_target \
  --holdings '{"SH600000": 1000}' \
  --prices '{"SH600000": 10.0}' \
  --route True --confirm_live True --wait True
```

`live_state` 不连接券商，只读取最近一次 runtime 快照。`live_daemon_*` 是 vn.py
no-UI/daemon 模式在 AlphaPilot 里的对应层：子进程持有 gateway/OMS/session，
周期写 `runtime_state.json`，Portal 和 CLI 都可以读取它。`live_daemon_halt/resume/refresh/order/submit_target`
以及 `live_daemon_cancel/reconnect` 通过 `runtime_commands.jsonl` 发进程内控制命令，由 daemon 心跳消费并回写
`last_command`，适合做急停、恢复、账户/持仓刷新，以及显式人工/策略目标路由。
`live_daemon_subscribe` 使用相同命令通道增量添加 observer 行情；它不触发下单或路由授权，
也不要求 `confirm_live`。正式 deployment 使用 `trading_deployment_subscribe`，由
`instance_id` 定位隔离 runtime。SDK 接受订阅后仍要通过 `awaiting_first_tick` 区分是否
真正收到 Tick。
撤单默认只撤 OMS 中仍处于 active 的委托；恢复时确实需要直发券商撤单时可传
`--force True --symbol <symbol>`。重连默认 `auto_resume=False`：连接、查询和 recovery
完成后仍保持 halted，需要人工检查后再 `live_daemon_resume`。路由结果会报告 `planned/submitted/unrouted/fully_routed`，风控拒单会留在 ledger
并让 daemon 命令返回 `ok=False` 或 `fully_routed=False`。0.2.0 后 `live_daemon_start` 不再接受
匿名策略名和参数；自动策略由 `DeploymentCoordinator` 以持久化 `strategy_instance_id` 启动，
产生的委托仍统一走 `LiveEngine.submit -> RiskGate -> BrokerGateway`。`trading_pause` 只暂停该实例
的新决策并尽力撤销其活动委托，不会断开行情和 daemon；`trading_stop` 同时撤销路由授权。
`live_order` 仅用于人工调试，
真实 live 同样需要 `--confirm_live True`。默认 `dry_run` 不会路由任何订单。

daemon 还会维护 `runtime_command_status.jsonl`，记录命令从 `accepted` 到
`processing` 再到 `done/failed` 的生命周期；`live_daemon_status` 会返回最近的
`command_status_tail`，`--wait True` 同时参考 `last_command` 和命令状态日志，避免
状态文件刷新竞态。`live_daemon_order` 会在命令结果里写入 `order_ack`、
`order_acknowledged`、`order_status`、`order_active`；`live_daemon_cancel` 会写入
`cancel_confirmation`、`cancel_confirmed`、`cancel_terminal`。`event_timeout` 控制
daemon 在命令处理期间等待券商异步回报的时间，方便把“请求已发送”和“券商已确认”
分开判断。runtime 每次连接后会执行一次基础 recovery：重新查询券商
账户/持仓，并尽力查询当日委托/成交；不同 broker 的不支持项会进入
`broker_refresh_unsupported`，查询异常会进入 `broker_refresh.errors`。recovery 还会
从当日 ledger 的 `submit` 事件恢复 `RiskGate` 的订单数、成交额和 client reference
去重状态，并生成 broker/ledger 对账摘要，标记 `missing_broker_order_ids`、
`external_broker_order_ids` 和恢复后仍活跃的委托。
CLI/API 可以用 `live_risk_status` / `/api/live/risk/status` 查看风控和恢复摘要，
用 `live_ledger_events` / `/api/live/ledger/events` 按 `kind`、`order_id`、
`reference` 或 `command_id` 查询审计日志。SDK 交易/行情断线会作为
`gateway_disconnected` 进入同一 ledger；交易断线还会额外产生 `disconnected`
并保持 halted。

Portal 后端也暴露同一套控制面：

- `GET /api/live/runtime/state`：读取最近一次状态，不连接券商；
- `POST /api/live/runtime/preflight`：检查 broker 注册、SDK、env、可选网络；
- `POST /api/live/runtime/connect`：一次性连接验证，只登录和查询，不下单；
- `GET/POST /api/live/daemon/{status,start,stop}`：管理长驻 runtime daemon；
- `POST /api/live/daemon/subscribe`：为独立 daemon 增量添加 observer 标的；
- `POST /api/live/daemon/{halt,resume,refresh,reconnect,cancel}`：向长驻 daemon 发送进程内控制命令；
- `POST /api/live/daemon/{order,submit-target}`：显式向长驻 daemon 发送下单/目标组合命令；
- `/api/live/paper/*`：纸面账户演练，内部同样复用 `LiveRuntime`。
- `/api/trading/deployments/{id}/{start,pause,reconcile,resume,stop,status}`：正式自动策略生命周期。
- `POST /api/trading/deployments/{id}/observer-subscriptions` 与对应 market snapshot/bars：
  为正式部署添加和查看隔离的 observer 行情。

Portal 前端的“实盘交易”页面已经接入这些能力：runtime 预检/连接、daemon 启停、
急停/恢复/刷新/保守重连、活动委托撤单、正式 deployment 生命周期、风控/恢复摘要、
命令流水和 ledger 事件查询都在同一个操作台里。真实 live 模式的连接与策略启动
会弹确认；策略产生的委托仍然进入 `LiveEngine.submit -> RiskGate -> BrokerGateway`
这条统一链路。

## 5. 策略接入（策略实例 → 实盘）

正式链路为：已完成 Bar → `SignalProvider` → `PortfolioPolicy` → `PortfolioDecision` → D+1
`AccountSizer` → 可恢复 `ExecutionPlan` → OMS → Risk → Broker。规则择时 v1 会由兼容 provider
包装进这条链路；v2 provider 直接实现生命周期。策略代码不能访问 Broker，也不能自行发送订单。

REPLAY 只由回测创建。已验证的日频 A 股/ETF 实例可以在 daemon 停止时直接配置为
PAPER、SIMULATION、SHADOW 或 LIVE；模式切换统一重新 PUT 部署。daemon 只是
`RuntimeControlPort` 的实现细节，只接受已验证且部署绑定未过期的实例 ID。重启后 LIVE 固定
进入待对账，必须由 reconcile/resume 流程恢复，不能通过匿名 runner 绕过部署状态。

## 常见失败

| 现象 | 含义 | 处理 |
|---|---|---|
| `sdk=x86_64, host=aarch64` | 宿主架构不能加载 SDK | 使用 `linux/amd64` 环境 |
| `sdk_bindings=MISSING` | pybind 绑定或插件未安装 | 重新安装 `alphapilot_xtpx` 和 `alphapilot-broker-xtp` |
| `endpoint ... timed out` | IP/端口不可达（测试环境周末/夜间可能下线） | 交易日重试；以券商邮件里的专属 IP/端口为准 |
| 缺少 `ALPHAPILOT_LIVE_XTP_*` | 必填环境变量未配置 | 补 `.env` 或 `docker compose run -e` |
| 登录返回 `10200000/10210000` | 到达服务器但认证服务离线 | 测试环境服务时段问题，交易日 9:15 后重试 |
| 交易登录 `12130005`，行情登录正常 | XTP 交易网关登录失败，常见原因是 `CLIENT_ID` 会话占用 | 换一个未占用的 `ALPHAPILOT_LIVE_XTP_CLIENT_ID` 后重试 |
| daemon smoke 拒单 `session: ... post_close` | AlphaPilot 风控认为当前不可报单 | 在交易/测试柜台可报单时段重跑，或先用直连 smoke 排查 SDK 连通性 |
| daemon smoke 拒单 `max_position_pct` | 测试标的 1 手名义金额相对账户权益/已有持仓过高 | 换低价 ETF/低名义金额标的；不要为了 smoke 放宽生产风控 |

## 当前实现文件

- `plugins/alphapilot_broker_xtp`：XTP Pro 原生网关插件
- `plugins/alphapilot_broker_emt`：EMT 原生网关插件
- `docs/live-plugins.md`：插件协议、安装和开发说明
- `plugins/alphapilot_broker_xcommon`：XTP 系共享映射表/转换器/回调状态机
- `alphapilot/systems/live/brokers/base.py`：SdkBrokerGateway（分发/轮询/SDK 日志）
- `alphapilot/systems/live/dispatch.py`：EventDispatcher（回调串行化 + 定时任务）
- `alphapilot/systems/live/bars.py` + `strategy_runner.py`、
  `alphapilot/systems/timing/live_adapter.py`：策略接入层
- `alphapilot/systems/live/brokers/registry.py`：entry point 发现、目录和 gateway 工厂
- `alphapilot/systems/live/brokers/vnpy_adapter.py`：保留的 vn.py 兼容桥
  （仅当以后要接 vn.py 系网关时才需要装 vnpy）
- `scripts/live_preflight_xtp.py` / `live_smoke_connect_xtp.py` /
  `live_smoke_daemon_xtp.py` /
  `live_smoke_import.py` / `live_x86_check.py`
- `Dockerfile.live`：编译两套 SDK 绑定（无 vn.py）
- `alphapilot_xtpx/`、`alphapilot_emt/`：仅提供编译绑定（`*.api`）、券商 SDK
  头文件和动态库，不包含 gateway 注册或运行入口
