# XTP Pro / EMT 实盘接入（原生网关，无 vn.py）

AlphaPilot 的实盘栈已完全去除 vn.py 依赖。券商接入方式：

- **XTP Pro (XTPX 1.2.1)**：`alphapilot/systems/live/brokers/xtp_pro.py` 原生网关，
  底层只用编译好的 `vnpy_xtp.api` pybind 绑定（该子包不依赖 vn.py）。
- **EMT（东方财富）**：`alphapilot/systems/live/brokers/emt.py` 原生网关，底层
  `vnpy_emt.api`。
- 两家共享 `brokers/vendor_common.py`（映射表 + 转换器 + 回调状态机）与
  `brokers/base.py` 的 `SdkBrokerGateway`（分发线程、轮询、SDK 日志目录）。
  **接入新券商 = 子类化 SdkBrokerGateway + 写映射表/转换回调**，OMS、风控、
  执行器、引擎全部复用。

线程模型：SDK 的 C++ 回调线程一进来就把处理体投递到网关的
`EventDispatcher`（单分发线程），OMS 及以上保持无锁单写者约束；资金/持仓的
2 秒轮询也在同一分发线程上执行。

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
pip install --no-build-isolation ./vnpy_xtp   # XTP Pro 绑定
pip install --no-build-isolation ./vnpy_emt   # EMT 绑定
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
`ALPHAPILOT_LIVE_<BROKER>_SETTING_JSON` 可整体覆盖。SDK 自身日志目录由
`ALPHAPILOT_SDK_LOG_DIR` 控制（默认 `~/.alphapilot/sdk_logs/<broker>`）。

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

## 4. 策略接入（择时 → 实盘）

链路：tick → `live/bars.py BarAggregator` → `timing/live_adapter.py
BatchStrategyAdapter`（现有 8 个规则策略零改动包装，信号翻转才发意图）→
`live/executor.orders_from_intents` → `LiveEngine.submit`（风控网关）。

`live/strategy_runner.py LiveTimingRunner` 两种驱动模式：

- `freq="day"`（默认）：收盘日线出信号，次日开盘集合竞价通过
  `CallAuctionAlgo` 执行 —— 与回测 `shift(1)` 语义一致；
- `freq="min"`：tick 聚合分钟 bar，bar 收盘立即提交。

外层循环周期性调用 `runner.step()`（可挂到 `EventDispatcher.add_periodic`）。

## 常见失败

| 现象 | 含义 | 处理 |
|---|---|---|
| `sdk=x86_64, host=aarch64` | 宿主架构不能加载 SDK | 使用 `linux/amd64` 环境 |
| `sdk_bindings=MISSING` | pybind 绑定未编译安装 | 重新 `pip install --no-build-isolation ./vnpy_xtp` |
| `endpoint ... timed out` | IP/端口不可达（测试环境周末/夜间可能下线） | 交易日重试；以券商邮件里的专属 IP/端口为准 |
| 缺少 `ALPHAPILOT_LIVE_XTP_*` | 必填环境变量未配置 | 补 `.env` 或 `docker compose run -e` |
| 登录返回 `10200000/10210000` | 到达服务器但认证服务离线 | 测试环境服务时段问题，交易日 9:15 后重试 |

## 当前实现文件

- `alphapilot/systems/live/brokers/xtp_pro.py`：XTP Pro 原生网关
- `alphapilot/systems/live/brokers/emt.py`：EMT 原生网关
- `alphapilot/systems/live/brokers/vendor_common.py`：XTP 系共享映射表/转换器/回调状态机
- `alphapilot/systems/live/brokers/base.py`：SdkBrokerGateway（分发/轮询/SDK 日志）
- `alphapilot/systems/live/dispatch.py`：EventDispatcher（回调串行化 + 定时任务）
- `alphapilot/systems/live/bars.py` + `strategy_runner.py`、
  `alphapilot/systems/timing/live_adapter.py`：策略接入层
- `alphapilot/systems/live/brokers/registry.py`：broker 注册 + `create_gateway()` 工厂
- `alphapilot/systems/live/brokers/vnpy_adapter.py`：保留的 vn.py 兼容桥
  （仅当以后要接 vn.py 系网关时才需要装 vnpy）
- `scripts/live_preflight_xtp.py` / `live_smoke_connect_xtp.py` /
  `live_smoke_import.py` / `live_x86_check.py`
- `Dockerfile.live`：编译两套 SDK 绑定（无 vn.py）
- `vnpy_xtp/`、`vnpy_emt/`：仅提供编译绑定（`*.api`）；其中 `gateway/` 目录为
  上游参考源码，不再安装、不再被引用
