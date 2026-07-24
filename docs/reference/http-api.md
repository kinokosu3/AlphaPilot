# HTTP API 完整参考

> 本文件由 `scripts/generate_docs_reference.py` 从 FastAPI OpenAPI 生成，请勿手工编辑。

当前共有 **133** 条路径、**150** 个操作。运行 Portal 后可访问 `/docs` 查看请求和响应 Schema。

Portal 默认只监听 `127.0.0.1`。下表中的“本机 Portal”不等于互联网级认证边界。

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
| `GET` | `/api/live/brokers` | Live Brokers | 本机 Portal |
| `POST` | `/api/live/daemon/cancel` | Live Daemon Cancel | 本机运维边界 |
| `POST` | `/api/live/daemon/halt` | Live Daemon Halt | 本机运维边界 |
| `POST` | `/api/live/daemon/order` | Live Daemon Order | 本机运维边界 |
| `POST` | `/api/live/daemon/reconnect` | Live Daemon Reconnect | 本机运维边界 |
| `POST` | `/api/live/daemon/refresh` | Live Daemon Refresh | 本机运维边界 |
| `POST` | `/api/live/daemon/resume` | Live Daemon Resume | 本机运维边界 |
| `POST` | `/api/live/daemon/start` | Live Daemon Start | 本机运维边界 |
| `GET` | `/api/live/daemon/status` | Live Daemon Status | 本机 Portal |
| `POST` | `/api/live/daemon/stop` | Live Daemon Stop | 本机运维边界 |
| `POST` | `/api/live/daemon/submit-target` | Live Daemon Submit Target | 本机运维边界 |
| `GET` | `/api/live/ledger/events` | Live Ledger Events | 本机 Portal |
| `GET` | `/api/live/market/bars` | Live Market Bars | 本机 Portal |
| `GET` | `/api/live/market/snapshot` | Live Market Snapshot | 本机 Portal |
| `POST` | `/api/live/paper/connect` | Live Paper Connect | 本机运维边界 |
| `POST` | `/api/live/paper/halt` | Live Paper Halt | 本机运维边界 |
| `POST` | `/api/live/paper/order` | Live Paper Order | 本机运维边界 |
| `POST` | `/api/live/paper/reset` | Live Paper Reset | 本机运维边界 |
| `POST` | `/api/live/paper/resume` | Live Paper Resume | 本机运维边界 |
| `GET` | `/api/live/paper/state` | Live Paper State | 本机 Portal |
| `POST` | `/api/live/paper/submit-target` | Live Paper Submit Target | 本机运维边界 |
| `GET` | `/api/live/plugins` | Live Plugins | 本机 Portal |
| `GET` | `/api/live/quote-providers` | Live Quote Providers | 本机 Portal |
| `GET` | `/api/live/risk/status` | Live Risk Status | 本机 Portal |
| `POST` | `/api/live/runtime/connect` | Live Runtime Connect | 本机运维边界 |
| `POST` | `/api/live/runtime/preflight` | Live Runtime Preflight | 本机运维边界 |
| `GET` | `/api/live/runtime/state` | Live Runtime State | 本机 Portal |
| `GET` | `/api/live/status` | Live Status | 本机 Portal |

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
| `GET` | `/api/trading/audit-events` | Trading Audit Events | 本机 Portal |
| `GET` | `/api/trading/backtest-runs/{run_id}` | Trading Backtest Run Get | 本机 Portal |
| `POST` | `/api/trading/backtest-runs/{run_id}/cancel` | Trading Backtest Run Cancel | Operator Bearer |
| `GET` | `/api/trading/backtest-runs/{run_id}/detail` | Trading Backtest Run Detail | 本机 Portal |
| `GET` | `/api/trading/broker-uat-runs` | Trading Broker Uat Runs | 本机 Portal |
| `GET` | `/api/trading/broker-uat-runs/{run_id}` | Trading Broker Uat Run | 本机 Portal |
| `GET` | `/api/trading/compatibility` | Trading Compatibility | 本机 Portal |
| `GET` | `/api/trading/decision-comparisons/{comparison_id}` | Trading Decision Comparison | 本机 Portal |
| `GET` | `/api/trading/deployments` | Trading Deployments | 本机 Portal |
| `GET` | `/api/trading/deployments/{instance_id}` | Trading Deployment | 本机 Portal |
| `PUT` | `/api/trading/deployments/{instance_id}` | Trading Deployment Update | 本机 Portal |
| `GET` | `/api/trading/deployments/{instance_id}/decision-comparisons` | Trading Decision Comparisons | 本机 Portal |
| `POST` | `/api/trading/deployments/{instance_id}/decision-comparisons` | Trading Decision Comparison Create | 本机 Portal |
| `GET` | `/api/trading/deployments/{instance_id}/diagnostics` | Trading Deployment Diagnostics | 本机 Portal |
| `POST` | `/api/trading/deployments/{instance_id}/pause` | Trading Deployment Pause | 本机 Portal |
| `POST` | `/api/trading/deployments/{instance_id}/reconcile` | Trading Deployment Reconcile | 本机 Portal |
| `POST` | `/api/trading/deployments/{instance_id}/resume` | Trading Deployment Resume | 本机 Portal |
| `POST` | `/api/trading/deployments/{instance_id}/start` | Trading Deployment Start | 本机 Portal |
| `GET` | `/api/trading/deployments/{instance_id}/status` | Trading Deployment Status | 本机 Portal |
| `POST` | `/api/trading/deployments/{instance_id}/stop` | Trading Deployment Stop | 本机 Portal |
| `GET` | `/api/trading/kill-switches` | Trading Kill Switches | 本机 Portal |
| `POST` | `/api/trading/kill-switches/{scope_type}/{scope_id}/{action}` | Trading Kill Switch | Operator Bearer |
| `GET` | `/api/trading/portfolio-policy-definitions` | Trading Portfolio Policy Definitions | 本机 Portal |
| `GET` | `/api/trading/strategy-definitions` | Trading Strategy Definitions | 本机 Portal |
| `GET` | `/api/trading/strategy-instances` | Trading Strategy Instances | 本机 Portal |
| `POST` | `/api/trading/strategy-instances` | Trading Strategy Instance Create | Operator Bearer |
| `POST` | `/api/trading/strategy-instances/from-research-asset` | Trading Strategy Instance From Research | Operator Bearer |
| `PATCH` | `/api/trading/strategy-instances/{instance_id}` | Trading Strategy Instance Update | Operator Bearer |
| `POST` | `/api/trading/strategy-instances/{instance_id}/backtest-runs` | Trading Backtest Run Create | Operator Bearer |
| `POST` | `/api/trading/strategy-instances/{instance_id}/preview` | Trading Strategy Instance Preview | Operator Bearer |
| `POST` | `/api/trading/strategy-instances/{instance_id}/validate` | Trading Strategy Instance Validate | Operator Bearer |
