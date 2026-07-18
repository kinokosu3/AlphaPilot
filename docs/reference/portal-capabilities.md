# Portal 功能矩阵

> 页面清单由 `docs/catalog.json` 管理，并与 React Router 路由进行一致性校验。

| 页面 | 路由 | 主要功能 | CLI 等价入口 | HTTP 领域 | 能力关系 | 使用说明 | 截图 |
|---|---|---|---|---|---|---|---|
| 首页 | `/` | 服务状态、快速入口、最近挖掘记录 | `modules、list_mine_logs` | `/api/status、/api/mining` | Portal 聚合视图；CLI 分项等价 | [打开](../user/portal-overview.md) | [查看](../assets/portal/home.png) |
| 因子挖掘 | `/mining` | LLM、AFF、GP、RL 挖掘与会话日志 | `mine、mine_aff、mine_gp、mine_rl` | `/api/jobs、/api/mining` | CLI/API 等价；Portal 提供交互日志 | [打开](../user/factor-mining-and-library.md) | [查看](../assets/portal/mining.png) |
| 回测 | `/backtest` | 因子/策略回测、工作区、图表与排行榜 | `backtest、strategy_backtest` | `/api/jobs、/api/backtests` | 计算能力等价；图表为 Portal 展示 | [打开](../user/strategy-assets-and-backtests.md) | [查看](../assets/portal/backtest.png) |
| 择时（策略实例） | `/timing` | 定义、政策、实例、研究资产导入、preview 与统一回测 | `trading_*` | `/api/trading` | CLI/API 等价 | [打开](../user/strategy-instances.md) | [查看](../assets/portal/strategy-instances.png) |
| 因子与策略库 | `/library` | 因子、分类、研报提取、策略资产与导入导出 | `factor_*、strategy_*` | `/api/factors、/api/strategies、/api/report-factors` | PDF 草稿复核为 Portal 优先，其余可组合调用 | [打开](../user/factor-mining-and-library.md) | [查看](../assets/portal/library.png) |
| 行情数据 | `/market` | 数据任务、单股维护、股票池和 K 线 | `prepare_data、pool_*` | `/api/data、/api/market、/api/modules/run` | 数据操作等价；K 线为 Portal/回退 UI | [打开](../user/data-and-pools.md) | [查看](../assets/portal/market.png) |
| 每日交易 | `/daily-trade` | 滚动会话、每日信号、资金调整、历史和净值 | `daily_signals、trade_session_*` | `/api/daily-trade、/api/trade-sessions` | CLI/API 等价 | [打开](../user/daily-trade.md) | [查看](../assets/portal/daily-trade.png) |
| 模拟与实盘 | `/live` | runtime、daemon、手工交易、部署、证据、对账和 kill switch | `live_*、trading_*` | `/api/live、/api/trading` | CLI/API 等价；Portal 强化确认与观察 | [打开](../user/live-trading.md) | [查看](../assets/portal/live.png) |
| 调度 | `/scheduler` | 计划任务 CRUD、立即运行和调度 daemon | `scheduler（仅 daemon）` | `/api/schedules` | 计划 CRUD 为 Portal/API-only | [打开](../user/scheduling-notifications-and-operations.md) | [查看](../assets/portal/scheduler.png) |
| 通知 | `/notifications` | 渠道配置、测试、命令接收、配对和事件 | `notify_commands（仅接收器）` | `/api/notify` | 配置/测试/配对为 Portal/API-only | [打开](../user/scheduling-notifications-and-operations.md) | [查看](../assets/portal/notifications.png) |
| 高级设置 | `/advanced` | Portal/环境配置、日志清理和模块调用 | `timezone、clean_logs、modules、portal_restart` | `/api/portal、/api/logs、/api/modules` | 可组合 CLI 等价；表单为 Portal 展示 | [打开](../user/scheduling-notifications-and-operations.md) | [查看](../assets/portal/advanced.png) |

## 接口边界

- Portal 是现有系统和模块的操作界面，不另外实现交易或研究逻辑。
- `/api/trading` 承担正式策略实例与部署控制；`/api/live` 承担运行时和人工运维。
- UAT 只能由本地 CLI 发起，Portal 仅展示 UAT 结果。
- 高级设置中的 `/api/modules/run` 是本机运维入口，不应暴露到不可信网络。
