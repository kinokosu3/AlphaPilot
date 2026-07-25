# 组件与文档覆盖矩阵

> 本文件由 `scripts/generate_docs_reference.py` 生成。组件与文档映射来自 `docs/catalog.json`。

## 系统

| 系统 | 实现 | 用户说明 | 开发文档 |
|---|---|---|---|
| `data` | `alphapilot.systems.data.service.QlibDataSystem` | [打开](../user/data-and-pools.md) | [打开](../developer/systems/data.md) |
| `factor` | `alphapilot.systems.factor.service.FactorSystem` | [打开](../user/factor-mining-and-library.md) | [打开](../developer/systems/factor.md) |
| `strategy` | `alphapilot.systems.strategy.service.StrategySystem` | [打开](../user/strategy-assets-and-backtests.md) | [打开](../developer/systems/strategy.md) |
| `backtest` | `alphapilot.systems.backtest.service.QlibBacktestSystem` | [打开](../user/strategy-assets-and-backtests.md) | [打开](../developer/systems/backtest.md) |
| `notify` | `alphapilot.systems.notify.service.NotificationSystem` | [打开](../user/scheduling-notifications-and-operations.md) | [打开](../developer/systems/notify.md) |
| `live` | `alphapilot.systems.live.service.LiveSystem` | [打开](../user/live-trading.md) | [打开](../developer/systems/live.md) |
| `trading` | `alphapilot.systems.trading.service.TradingStrategySystem` | [打开](../user/strategy-instances.md) | [打开](../developer/systems/trading.md) |

## 模块

| 模块 | CLI 数量 | 用户说明 | 开发文档 |
|---|---:|---|---|
| `alpha_mining` | 6 | [打开](../user/factor-mining-and-library.md) | [打开](../developer/modules/alpha-mining.md) |
| `report_factor` | 0 | [打开](../user/factor-mining-and-library.md) | [打开](../developer/modules/report-factor.md) |
| `platform` | 9 | [打开](../user/scheduling-notifications-and-operations.md) | [打开](../developer/modules/platform.md) |
| `data_viz` | 1 | [打开](../user/data-and-pools.md) | [打开](../developer/modules/data-viz.md) |
| `portal` | 6 | [打开](../user/portal-overview.md) | [打开](../developer/modules/portal.md) |
| `backtest_viz` | 1 | [打开](../user/strategy-assets-and-backtests.md) | [打开](../developer/modules/backtest-viz.md) |
| `qlib_yaml` | 2 | [打开](../user/strategy-assets-and-backtests.md) | [打开](../developer/modules/qlib-yaml.md) |
| `daily_trade` | 8 | [打开](../user/daily-trade.md) | [打开](../developer/modules/daily-trade.md) |
| `factor` | 12 | [打开](../user/factor-mining-and-library.md) | [打开](../developer/modules/factor.md) |
| `alphaforge_aff` | 1 | [打开](../user/factor-mining-and-library.md) | [打开](../developer/modules/alphaforge-aff.md) |
| `alphaforge_search` | 2 | [打开](../user/factor-mining-and-library.md) | [打开](../developer/modules/alphaforge-search.md) |
| `stock_pool` | 10 | [打开](../user/data-and-pools.md) | [打开](../developer/modules/stock-pool.md) |
| `strategy_backtest` | 3 | [打开](../user/strategy-assets-and-backtests.md) | [打开](../developer/modules/strategy-backtest.md) |
| `live` | 27 | [打开](../user/live-trading.md) | [打开](../developer/modules/live.md) |
| `trading_cli` | 32 | [打开](../user/strategy-instances.md) | [打开](../developer/modules/trading-cli.md) |

## 非注册策略子系统

- [择时 provider](../developer/subsystems/timing.md)
- [Qlib 横截面选股](../developer/subsystems/selection.md)
- [研究门禁与证据](../developer/subsystems/research.md)
- [自定义 Provider、PortfolioPolicy 与 artifact](../developer/strategy-extension.md)
