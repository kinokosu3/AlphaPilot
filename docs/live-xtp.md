# 普通 XTP 实盘/仿真接入

本文说明 AlphaPilot 当前的普通 XTP 接口路径，不包含 XTP Pro。普通 XTP 通过
`vnpy_xtp:XtpGateway` 接入，AlphaPilot 上层通过 `VnpyBrokerAdapter("XTP")`
驱动，因此 OMS、风险控制、执行器不需要知道底层券商 SDK 细节。

## 运行环境

普通 XTP Linux SDK 是 `x86_64` 动态库，必须在 `linux/amd64` 环境运行。推荐使用
`Dockerfile.live`：

```bash
docker compose --profile live build live
```

在 Apple Silicon / arm64 Linux 上直接运行宿主 Python 会失败，因为本仓库 vendored 的
`libxtpquoteapi.so` 和 `libxtptraderapi.so` 不是 arm64 库。Docker Compose 已为 live
服务固定 `platform: linux/amd64`。

## 环境变量

真实凭证只放在私有 `.env` 或 `docker compose run -e ...` 参数里，不要提交到仓库。

```bash
ALPHAPILOT_LIVE_MODE=live
ALPHAPILOT_LIVE_BROKER=xtp
ALPHAPILOT_LIVE_XTP_ACCOUNT=<your_xtp_account>
ALPHAPILOT_LIVE_XTP_PASSWORD=<your_xtp_password>
ALPHAPILOT_LIVE_XTP_CLIENT_ID=1
ALPHAPILOT_LIVE_XTP_SOFTWARE_KEY=<your_xtp_software_key>
ALPHAPILOT_LIVE_XTP_QUOTE_HOST=<quote_host_from_xtp_email>
ALPHAPILOT_LIVE_XTP_QUOTE_PORT=<quote_port_from_xtp_email>
ALPHAPILOT_LIVE_XTP_TRADE_HOST=<trade_host_from_xtp_email>
ALPHAPILOT_LIVE_XTP_TRADE_PORT=<trade_port_from_xtp_email>
ALPHAPILOT_LIVE_XTP_QUOTE_PROTOCOL=TCP
ALPHAPILOT_LIVE_XTP_LOG_LEVEL=INFO
```

如果测试账号邮件没有给行情/交易地址，可以临时给脚本传
`--use-public-test-endpoints`，它会填入历史公开仿真端点。但这些端点可能失效；
以 XTP 账号申请邮件里的 IP/端口为准。

## 1. 预检

预检不登录、不发送账号密码、不下单，只检查环境变量、SDK 架构、gateway import
和 TCP 端点连通性。

```bash
docker compose --profile live run --rm live \
  python scripts/live_preflight_xtp.py --timeout 5
```

如果没有专属 IP/端口，先用公开端点兜底：

```bash
docker compose --profile live run --rm live \
  python scripts/live_preflight_xtp.py --use-public-test-endpoints --timeout 5
```

预检通过后再进行登录 smoke。

## 2. 查询型 smoke

默认 smoke 只做：

- 交易和行情登录
- 等待账户快照
- 等待合约加载
- 订阅一个标的并等待 tick

它默认不下单。

```bash
docker compose --profile live run --rm live \
  python scripts/live_smoke_connect_xtp.py \
    --symbol 600000 \
    --timeout 30 \
    --dump-logs
```

使用公开端点兜底：

```bash
docker compose --profile live run --rm live \
  python scripts/live_smoke_connect_xtp.py \
    --use-public-test-endpoints \
    --symbol 600000 \
    --timeout 30 \
    --dump-logs
```

如果只是想验证交易登录和资金/持仓查询，暂时不检查行情 tick：

```bash
docker compose --profile live run --rm live \
  python scripts/live_smoke_connect_xtp.py \
    --use-public-test-endpoints \
    --skip-tick \
    --timeout 30 \
    --dump-logs
```

## 3. 小单下单/撤单 smoke

显式加 `--order` 才会下单。脚本会在有 tick 的前提下，下一个 1 手、约低于最新价
10% 的限价买单，然后等待委托回报并撤单。

```bash
docker compose --profile live run --rm live \
  python scripts/live_smoke_connect_xtp.py \
    --use-public-test-endpoints \
    --symbol 600000 \
    --timeout 30 \
    --dump-logs \
    --order
```

仿真账户也建议先跑查询型 smoke，确认资金、合约和行情都正常后再打开 `--order`。

## 常见失败

| 现象 | 含义 | 处理 |
|---|---|---|
| `sdk=x86_64, host=aarch64` | 宿主架构不能加载普通 XTP SDK | 使用 `linux/amd64` live 镜像 |
| `vnpy_xtp gateway import` 失败 | C++ gateway 未编译/动态库未加载 | 重新 build `Dockerfile.live` |
| `endpoint ... timed out` | IP/端口不可达或公开端点失效 | 使用申请邮件里的专属 IP/端口 |
| 缺少 `ALPHAPILOT_LIVE_XTP_*` | 必填环境变量未配置 | 补 `.env` 或 `docker compose run -e` |

## 当前实现文件

- `alphapilot/systems/live/brokers/registry.py`：XTP broker 注册与 env -> vn.py 设置转换
- `alphapilot/systems/live/brokers/vnpy_adapter.py`：AlphaPilot live port 到 vn.py gateway 的桥
- `scripts/live_preflight_xtp.py`：不登录的环境/端点预检
- `scripts/live_smoke_connect_xtp.py`：真实连接 smoke，默认查询型，`--order` 才下单
- `Dockerfile.live`：编译并安装 vendored `vnpy_xtp`
