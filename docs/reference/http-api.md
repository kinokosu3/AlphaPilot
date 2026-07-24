# HTTP API 完整参考

> 本文件由 `scripts/generate_docs_reference.py` 从 FastAPI OpenAPI 生成，请勿手工编辑。

当前共有 **134** 条路径、**151** 个操作。运行 Portal 后可访问 `/docs` 查看请求和响应 Schema。

Portal 默认只监听 `127.0.0.1`。交易写操作由启动时冻结的 `required | optional` 模式决定；`optional + 0.0.0.0 + wildcard CORS` 允许可达客户端无令牌写入，不等于互联网级认证边界。

## `backtests`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/backtests` | Backtests | 本机 Portal |
| `GET` | `/api/backtests/leaderboard` | Backtest Leaderboard | 本机 Portal |
| `GET` | `/api/backtests/leaderboards` | Backtest Leaderboards | 本机 Portal |
| `DELETE` | `/api/backtests/{workspace_id}` | Delete Backtest | 本机 Portal |
| `GET` | `/api/backtests/{workspace_id}` | Backtest Detail | 本机 Portal |

## `daily-trade`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `POST` | `/api/daily-trade` | Daily Trade | 本机 Portal |

## `data`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `POST` | `/api/data/actions` | Run Data Action | 本机 Portal |
| `GET` | `/api/data/instrument-sets` | Data Instrument Sets | 本机 Portal |
| `GET` | `/api/data/symbols` | Data Symbols | 本机 Portal |
| `POST` | `/api/data/symbols/apply-adjust` | Apply Adjust Symbol | 本机 Portal |
| `POST` | `/api/data/symbols/delete` | Delete Symbol | 本机 Portal |
| `POST` | `/api/data/symbols/refresh` | Refresh Symbol | 本机 Portal |
| `POST` | `/api/data/symbols/trim` | Trim Symbol | 本机 Portal |
| `GET` | `/api/data/universe` | Data Universe | 本机 Portal |

## `factors`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/factors` | List Factors | 本机 Portal |
| `POST` | `/api/factors` | Add Factor | 本机 Portal |
| `POST` | `/api/factors/backtest` | Backtest Factors | 本机 Portal |
| `POST` | `/api/factors/bulk-delete` | Bulk Delete Factors | 本机 Portal |
| `POST` | `/api/factors/categories` | Create Category | 本机 Portal |
| `POST` | `/api/factors/categories/bulk` | Bulk Factor Category | 本机 Portal |
| `DELETE` | `/api/factors/categories/{name}` | Delete Category | 本机 Portal |
| `PATCH` | `/api/factors/categories/{name}` | Rename Category | 本机 Portal |
| `GET` | `/api/factors/duplicates` | Factor Duplicates | 本机 Portal |
| `POST` | `/api/factors/export` | Export Factors | 本机 Portal |
| `POST` | `/api/factors/import` | Import Factors | 本机 Portal |
| `POST` | `/api/factors/validate` | Validate Factor | 本机 Portal |
| `DELETE` | `/api/factors/{factor_name}` | Delete Factor | 本机 Portal |
| `PATCH` | `/api/factors/{factor_name}` | Rename Factor | 本机 Portal |

## `jobs`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/jobs` | List Jobs | 本机 Portal |
| `POST` | `/api/jobs` | Start Job | 本机 Portal |
| `POST` | `/api/jobs/clear` | Clear Jobs | 本机 Portal |
| `DELETE` | `/api/jobs/{job_id}` | Delete Job | 本机 Portal |
| `POST` | `/api/jobs/{job_id}/cancel` | Cancel Job | 本机 Portal |
| `GET` | `/api/jobs/{job_id}/log` | Job Log | 本机 Portal |
| `GET` | `/api/jobs/{job_id}/progress` | Job Progress | 本机 Portal |
| `GET` | `/api/jobs/{job_id}/result` | Job Result | 本机 Portal |

## `live`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/live/brokers` | Live Brokers | 免 Operator token |
| `POST` | `/api/live/daemon/cancel` | Live Daemon Cancel | Portal operator auth（required / optional） |
| `POST` | `/api/live/daemon/halt` | Live Daemon Halt | Portal operator auth（required / optional） |
| `POST` | `/api/live/daemon/order` | Live Daemon Order | Portal operator auth（required / optional） |
| `POST` | `/api/live/daemon/reconnect` | Live Daemon Reconnect | Portal operator auth（required / optional） |
| `POST` | `/api/live/daemon/refresh` | Live Daemon Refresh | Portal operator auth（required / optional） |
| `POST` | `/api/live/daemon/resume` | Live Daemon Resume | Portal operator auth（required / optional） |
| `POST` | `/api/live/daemon/start` | Live Daemon Start | Portal operator auth（required / optional） |
| `GET` | `/api/live/daemon/status` | Live Daemon Status | 免 Operator token |
| `POST` | `/api/live/daemon/stop` | Live Daemon Stop | Portal operator auth（required / optional） |
| `POST` | `/api/live/daemon/submit-target` | Live Daemon Submit Target | Portal operator auth（required / optional） |
| `GET` | `/api/live/ledger/events` | Live Ledger Events | 免 Operator token |
| `GET` | `/api/live/market/bars` | Live Market Bars | 免 Operator token |
| `GET` | `/api/live/market/snapshot` | Live Market Snapshot | 免 Operator token |
| `POST` | `/api/live/paper/connect` | Live Paper Connect | Portal operator auth（required / optional） |
| `POST` | `/api/live/paper/halt` | Live Paper Halt | Portal operator auth（required / optional） |
| `POST` | `/api/live/paper/order` | Live Paper Order | Portal operator auth（required / optional） |
| `POST` | `/api/live/paper/reset` | Live Paper Reset | Portal operator auth（required / optional） |
| `POST` | `/api/live/paper/resume` | Live Paper Resume | Portal operator auth（required / optional） |
| `GET` | `/api/live/paper/state` | Live Paper State | 免 Operator token |
| `POST` | `/api/live/paper/submit-target` | Live Paper Submit Target | Portal operator auth（required / optional） |
| `GET` | `/api/live/plugins` | Live Plugins | 免 Operator token |
| `GET` | `/api/live/quote-providers` | Live Quote Providers | 免 Operator token |
| `GET` | `/api/live/risk/status` | Live Risk Status | 免 Operator token |
| `POST` | `/api/live/runtime/connect` | Live Runtime Connect | Portal operator auth（required / optional） |
| `POST` | `/api/live/runtime/preflight` | Live Runtime Preflight | 免 Operator token（只读探测） |
| `GET` | `/api/live/runtime/state` | Live Runtime State | 免 Operator token |
| `GET` | `/api/live/status` | Live Status | 免 Operator token |

## `logs`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `POST` | `/api/logs/cleanup` | Cleanup Logs | 本机 Portal |

## `market`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/market/kline` | Market Kline | 本机 Portal |
| `GET` | `/api/market/sources` | Market Sources | 本机 Portal |
| `GET` | `/api/market/symbols` | Market Symbols | 本机 Portal |

## `mining`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/mining/sessions` | Mining Sessions | 本机 Portal |
| `DELETE` | `/api/mining/sessions/{session_name}` | Delete Mining Session | 本机 Portal |
| `GET` | `/api/mining/sessions/{session_name}` | Mining Session Detail | 本机 Portal |
| `GET` | `/api/mining/sessions/{session_name}/files/{file_path}` | Mining Session File | 本机 Portal |

## `modules`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/modules` | Modules | 本机 Portal |
| `POST` | `/api/modules/run` | Run Module | 本机 Portal |

## `notify`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/notify` | Notify Config | 本机 Portal |
| `PATCH` | `/api/notify` | Update Notify | 本机 Portal |
| `POST` | `/api/notify/commands/dispatch` | Notify Commands Dispatch | 本机 Portal |
| `GET` | `/api/notify/commands/events` | Notify Commands Events | 本机 Portal |
| `POST` | `/api/notify/commands/pair-code` | Notify Commands Pair Code | 本机 Portal |
| `POST` | `/api/notify/commands/plan` | Notify Commands Plan | 本机 Portal |
| `POST` | `/api/notify/commands/register-menu` | Notify Commands Register Menu | 本机 Portal |
| `POST` | `/api/notify/commands/start` | Notify Commands Start | 本机 Portal |
| `GET` | `/api/notify/commands/status` | Notify Commands Status | 本机 Portal |
| `POST` | `/api/notify/commands/stop` | Notify Commands Stop | 本机 Portal |
| `POST` | `/api/notify/feishu/events` | Notify Feishu Events | 飞书回调校验 |
| `POST` | `/api/notify/test` | Test Notify | 本机 Portal |

## `portal`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/portal/env` | Get Portal Env | 本机 Portal |
| `PATCH` | `/api/portal/env` | Update Portal Env | 本机 Portal |
| `POST` | `/api/portal/restart` | Restart Portal | 本机 Portal |
| `GET` | `/api/portal/security` | Get Portal Security | 只读、免 Operator token |
| `GET` | `/api/portal/settings` | Get Portal Settings | 本机 Portal |
| `PATCH` | `/api/portal/settings` | Update Portal Settings | 本机 Portal |

## `report-factors`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `POST` | `/api/report-factors/commit` | Commit Report Factors | 本机 Portal |
| `POST` | `/api/report-factors/extract` | Extract Report Factors | 本机 Portal |
| `GET` | `/api/report-factors/ocr-providers` | Report Factor Ocr Providers | 本机 Portal |
| `POST` | `/api/report-factors/upload` | Upload Report Factor | 本机 Portal |
| `DELETE` | `/api/report-factors/uploads/{upload_id}` | Delete Report Factor Upload | 本机 Portal |

## `schedules`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/schedules` | List Schedules | 本机 Portal |
| `POST` | `/api/schedules` | Create Schedule | 本机 Portal |
| `GET` | `/api/schedules/daemon` | Scheduler Status | 本机 Portal |
| `POST` | `/api/schedules/daemon/start` | Scheduler Start | 本机 Portal |
| `POST` | `/api/schedules/daemon/stop` | Scheduler Stop | 本机 Portal |
| `DELETE` | `/api/schedules/{schedule_id}` | Delete Schedule | 本机 Portal |
| `PATCH` | `/api/schedules/{schedule_id}` | Update Schedule | 本机 Portal |
| `POST` | `/api/schedules/{schedule_id}/run` | Run Schedule | 本机 Portal |

## `static`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/branding/logo.svg` | Portal Logo | 本机 Portal |

## `status`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/status` | Status | 本机 Portal |

## `strategies`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/strategies` | List Strategies | 本机 Portal |
| `POST` | `/api/strategies` | Save Strategy | 本机 Portal |
| `POST` | `/api/strategies/export` | Export Strategy File | 本机 Portal |
| `POST` | `/api/strategies/from-factors` | Create Strategy From Factors | 本机 Portal |
| `POST` | `/api/strategies/import` | Import Strategy | 本机 Portal |
| `DELETE` | `/api/strategies/{strategy_name}` | Delete Strategy | 本机 Portal |
| `GET` | `/api/strategies/{strategy_name}/export` | Export Strategy | 本机 Portal |

## `trade-sessions`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/trade-sessions` | List Trade Sessions | 本机 Portal |
| `POST` | `/api/trade-sessions` | Create Trade Session | 本机 Portal |
| `DELETE` | `/api/trade-sessions/{name}` | Delete Trade Session | 本机 Portal |
| `GET` | `/api/trade-sessions/{name}` | Get Trade Session | 本机 Portal |
| `POST` | `/api/trade-sessions/{name}/cash` | Adjust Trade Session Cash | 本机 Portal |
| `GET` | `/api/trade-sessions/{name}/history` | Get Trade Session History | 本机 Portal |

## `trading`

| 方法 | 路径 | 用途 | 认证/边界 |
|---|---|---|---|
| `GET` | `/api/trading/audit-events` | Trading Audit Events | 免 Operator token |
| `GET` | `/api/trading/backtest-runs/{run_id}` | Trading Backtest Run Get | 免 Operator token |
| `POST` | `/api/trading/backtest-runs/{run_id}/cancel` | Trading Backtest Run Cancel | Portal operator auth（required / optional） |
| `GET` | `/api/trading/backtest-runs/{run_id}/detail` | Trading Backtest Run Detail | 免 Operator token |
| `GET` | `/api/trading/broker-uat-runs` | Trading Broker Uat Runs | 免 Operator token |
| `GET` | `/api/trading/broker-uat-runs/{run_id}` | Trading Broker Uat Run | 免 Operator token |
| `GET` | `/api/trading/compatibility` | Trading Compatibility | 免 Operator token |
| `GET` | `/api/trading/decision-comparisons/{comparison_id}` | Trading Decision Comparison | 免 Operator token |
| `GET` | `/api/trading/deployments` | Trading Deployments | 免 Operator token |
| `GET` | `/api/trading/deployments/{instance_id}` | Trading Deployment | 免 Operator token |
| `PUT` | `/api/trading/deployments/{instance_id}` | Trading Deployment Update | Portal operator auth（required / optional） |
| `GET` | `/api/trading/deployments/{instance_id}/decision-comparisons` | Trading Decision Comparisons | 免 Operator token |
| `POST` | `/api/trading/deployments/{instance_id}/decision-comparisons` | Trading Decision Comparison Create | Portal operator auth（required / optional） |
| `GET` | `/api/trading/deployments/{instance_id}/diagnostics` | Trading Deployment Diagnostics | 免 Operator token |
| `POST` | `/api/trading/deployments/{instance_id}/pause` | Trading Deployment Pause | Portal operator auth（required / optional） |
| `POST` | `/api/trading/deployments/{instance_id}/reconcile` | Trading Deployment Reconcile | Portal operator auth（required / optional） |
| `POST` | `/api/trading/deployments/{instance_id}/resume` | Trading Deployment Resume | Portal operator auth（required / optional） |
| `POST` | `/api/trading/deployments/{instance_id}/start` | Trading Deployment Start | Portal operator auth（required / optional） |
| `GET` | `/api/trading/deployments/{instance_id}/status` | Trading Deployment Status | 免 Operator token |
| `POST` | `/api/trading/deployments/{instance_id}/stop` | Trading Deployment Stop | Portal operator auth（required / optional） |
| `GET` | `/api/trading/kill-switches` | Trading Kill Switches | 免 Operator token |
| `POST` | `/api/trading/kill-switches/{scope_type}/{scope_id}/{action}` | Trading Kill Switch | Portal operator auth（required / optional） |
| `GET` | `/api/trading/portfolio-policy-definitions` | Trading Portfolio Policy Definitions | 免 Operator token |
| `GET` | `/api/trading/strategy-definitions` | Trading Strategy Definitions | 免 Operator token |
| `GET` | `/api/trading/strategy-instances` | Trading Strategy Instances | 免 Operator token |
| `POST` | `/api/trading/strategy-instances` | Trading Strategy Instance Create | Portal operator auth（required / optional） |
| `POST` | `/api/trading/strategy-instances/from-research-asset` | Trading Strategy Instance From Research | Portal operator auth（required / optional） |
| `PATCH` | `/api/trading/strategy-instances/{instance_id}` | Trading Strategy Instance Update | Portal operator auth（required / optional） |
| `POST` | `/api/trading/strategy-instances/{instance_id}/backtest-runs` | Trading Backtest Run Create | Portal operator auth（required / optional） |
| `POST` | `/api/trading/strategy-instances/{instance_id}/preview` | Trading Strategy Instance Preview | Portal operator auth（required / optional） |
| `POST` | `/api/trading/strategy-instances/{instance_id}/validate` | Trading Strategy Instance Validate | Portal operator auth（required / optional） |
