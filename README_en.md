<div align="center">

<img src="docs/AlphaPilot_logo.svg" alt="AlphaPilot" width="760">

### LLM-driven quantitative research, paper trading, and live execution platform

[中文](README.md)&nbsp;|&nbsp;[English](README_en.md)

`Multi-agent factor mining`&nbsp;·&nbsp;`Qlib backtesting`&nbsp;·&nbsp;`Quant timing`&nbsp;·&nbsp;`Paper / live trading`&nbsp;·&nbsp;`Web portal`&nbsp;·&nbsp;`Telegram / Feishu notifications`

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-22C55E">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white">
  <img alt="Notify" src="https://img.shields.io/badge/Notify-Telegram%20%7C%20Feishu-26A5E4?logo=telegram&logoColor=white">
</p>

[Quick Start](#-quick-start)&nbsp;·&nbsp;[Core Features](#core-features)&nbsp;·&nbsp;[Typical Workflow](#-typical-workflow)&nbsp;·&nbsp;[Docs](#-more-documentation)&nbsp;·&nbsp;[Docker Deployment](docs/DOCKER.md)

</div>

---

## Project Overview

AlphaPilot is a stock-focused quantitative research and trading platform covering data preparation, factor generation, backtest evaluation, strategy assets, daily signals, paper trading, and live execution. It uses an LLM-driven multi-agent pipeline for factor research, Qlib for backtesting and signal validation, and a unified Live Runtime that connects research signals to risk controls, order management, broker gateways, and an audit ledger. The Web portal centralizes data, tasks, research assets, notifications, and trading runtime status.

## Core Features

| Capability | Main Entry | Description |
|------------|------------|-------------|
| Factor mining | `alphapilot mine` | LLM multi-agent workflow plus formula-based AlphaForge / GP / RL / AFF methods |
| Backtest evaluation | `alphapilot backtest` | Portfolio backtests, per-factor IC screening, leaderboards, and return curves |
| Strategy creation | `alphapilot strategy_create` | Save selected factors as strategy assets with factors, model, rebalance settings, costs, and dates |
| Strategy retesting | `alphapilot strategy_backtest` | Reuse saved strategy assets and models for continued validation |
| Daily signals | `alphapilot daily_signals` | Advance positions by trading day and generate single-day rebalance signals |
| Trade sessions | `alphapilot trade_session_create` | Snapshot a strategy into a self-contained, resumable daily-trade account |
| Quant timing | `alphapilot trading_instance_create` / `trading_backtest` | Formal strategy instances, unified replay, and controlled deployment; legacy `timing_*` commands were removed in 0.2.0 |
| Paper / live trading | `alphapilot live_*` | `dry_run` / `paper` / `live` modes, unified risk and OMS, daemon control, recovery reconciliation, and an audit ledger; XTP Pro / EMT are optional plugins |
| Unified portal | `alphapilot portal` | Central UI for data, factors, backtests, timing, tasks, notifications, and live controls |
| Data preparation | `alphapilot prepare_data` | baostock / tushare to Qlib data pipeline |
| Notifications and remote control | `alphapilot notify_commands` | Task completion notifications through Telegram / Feishu / email plus remote chat commands |

### Factor Mining

AlphaPilot's primary workflow is automated factor research. You can start an LLM-driven multi-agent mining process with natural language, or use formula-based methods in the same project to generate candidate factors before validation, backtesting, and asset persistence.

- Unified management for the Idea Agent, Factor Agent, and Eval Agent research stages
- Supports `alphapilot mine` for LLM-driven factor mining
- Supports formula-based mining methods including GP, RL, and AFF
- Factors can be saved into the factor library and reused for backtests or strategy assets

Main entry: `alphapilot mine --direction "your market hypothesis"`

<div align="center">
  <img src="docs/mining.png" alt="Factor mining page: LLM factor mining and formula-based mining" width="860">
  <br><br>
  <img src="docs/factor_zoo.png" alt="Factor / strategy library: unified factor asset management" width="860">
</div>

### Backtesting and Evaluation

The project includes multiple backtesting and evaluation modes. It supports formal portfolio backtests and quick screening for large candidate factor sets. The README keeps only the common entries; see the [CLI command reference](docs/alphapilot-cli.md) for more commands and parameters.

- `multi_combined`: combine multiple factors, train a model, and run a portfolio backtest
- `single_ic`: quickly calculate IC, RankIC, and ICIR for each factor
- `multi_sequential`: run full portfolio backtests for factors one by one
- Portal backtest page: visualizes returns, excess returns, account composition, turnover, daily details, factor leaderboards, and benchmark comparisons

Main entry: `alphapilot backtest --factor_path /path/to/factors.csv`

<div align="center">
  <img src="docs/backtest.png" alt="Backtesting: cumulative returns, excess returns, and account composition" width="860">
</div>

### Strategy Retesting and Daily Signals

After a strategy asset has been saved, you can reuse existing factors and models for further validation without running the full mining pipeline again. For daily research or simulated trading scenarios, AlphaPilot can also generate single-day rebalance signals from saved strategies.

- `strategy_backtest` retests saved strategy assets
- `daily_signals` advances position state by a specified trading day
- `trade_session_create` / `trade_session_show` / `trade_session_history` manage self-contained daily-trade sessions with their own strategy snapshot, state, and history
- Suitable for model reuse, strategy revalidation, and single-day rebalance drills
- Results can flow back into strategy assets and the portal for unified review

Main entry: `alphapilot strategy_backtest --strategy_name "<strategy name>" --mode=retrain`

### Paper and Live Trading

The live subsystem connects target portfolios or timing signals to one execution path: `LiveRuntime → LiveEngine → RiskGate → BrokerGateway`. The default `dry_run` mode never routes orders, while `paper` is intended for local rehearsal. A command can route to a real broker only after `live` is selected and live execution is explicitly confirmed.

- Three run modes—`dry_run`, `paper`, and `live`—with foreground and persistent daemon operation
- Manual orders, cancellation, and target-portfolio submission; automated strategies start only through persistent instances and `trading_*` deployment controls
- A single pre-trade gate for session, board-lot, price, cash, position, concentration, per-order, and daily-turnover checks
- OMS state, append-only audit events, runtime snapshots, and recovery reconciliation; a trade-channel disconnect halts execution and requires review before resuming
- XTP Pro and EMT broker/quote integrations are decoupled from the core as installable pip plugins; trading and quote providers can be configured independently
- The Portal Live page exposes preflight, connection, daemon and strategy controls, risk state, orders, and ledger queries

The safest first step is a paper daemon:

```bash
alphapilot live_daemon_start --mode paper --symbols 600000,000001 --cash 100000
alphapilot live_daemon_status --mode paper
alphapilot live_daemon_stop --mode paper
```

> **Live-trading warning:** this subsystem is still under active development and broker-environment validation. Before using a real account, complete paper rehearsal, plugin and network preflight, a small broker-side acceptance test, and a review of risk limits and recovery results. Do not enable real routing until you understand `--confirm_live`, daemon state, and the audit ledger.

See [XTP Pro / EMT live setup](docs/live-xtp.md) and the [live plugin development and installation guide](docs/live-plugins.md) for the full setup.

### Unified Web Portal

AlphaPilot provides a unified Web portal for daily research and runtime operations. It brings data, factors, backtests, timing, tasks, notifications, and live controls into one interface, reducing context switching across scripts and standalone pages.

- Unified access to factor mining, backtesting, timing, strategy libraries, market data, notification settings, and live runtime status
- Supports background tasks, scheduled tasks, and result review
- Built-in backtest visualizations: cumulative returns, excess returns, account composition, turnover charts, date-range filters, daily details, factor leaderboards, and benchmark comparisons
- The Live page separates paper and live workspaces and exposes preflight, daemon, strategy, risk, and audit controls
- Suitable for both local research environments and server deployments

Main entry: `alphapilot portal`

<div align="center">
  <img src="docs/portal.png" alt="Portal home page and task panel" width="860">
</div>

### Data Preparation and Management

The project includes a complete A-share data preparation pipeline, from raw market data to Qlib data. Factor h5 cache is generated automatically by research and backtest tasks. This README keeps the shortest path; see the detailed documentation for data sources, adjustment modes, and advanced parameters.

- Supports baostock and tushare data sources
- Supports market data downloads, price adjustment, and Qlib conversion
- Supports stock pool management and single-stock data maintenance
- Connects directly with factor mining, backtesting, and daily signal generation

Main entry: `alphapilot prepare_data download --stock_csv important_data/stock_lists/main_stock_2026_4_27.csv`

<div align="center">
  <img src="docs/stock_zoo.png" alt="Market data: data actions, stock pools, and single-stock management" width="860">
  <br><br>
  <img src="docs/data_zoo.png" alt="Market data: local K-line viewer" width="860">
</div>

### Notifications and Remote Control

Research tasks often take a long time. AlphaPilot includes task notifications and a two-way chat command system: completed background tasks can proactively push results, and you can also start, query, and manage tasks remotely from chat tools without watching a terminal.

- Supports **Telegram, Feishu, and email** notification channels
- Automatically pushes task completion or all-task status updates
- Telegram / Feishu command receivers support `/mine`, `/backtest`, `/data`, `/status`, `/jobs`, `/cancel`, `/log`, `/result`, and more
- Whitelisted user authentication for remote task submission, log review, artifact lookup, and status checks
- Credentials can be configured in the portal notification page or injected through `ALPHAPILOT_NOTIFY_*` environment variables

Main entry: `alphapilot notify_commands --channel telegram`

<div align="center">
  <img src="docs/notification.png" alt="Notification settings and command receiver" width="860">
</div>

## 🚀 Quick Start

The following flow focuses on local installation and a minimal working loop. For **one-command Docker deployment**, see [docs/DOCKER.md](docs/DOCKER.md).

### 1. Create an Environment

```bash
conda create -n alphapilot python=3.11
conda activate alphapilot
```

### 2. Install the Project

```bash
git clone https://github.com/ai-yang/AlphaPilot.git
cd AlphaPilot
pip install -e .
```

If you need the Web portal frontend, install Node.js and build the frontend assets under `alphapilot/modules/portal/web`:

```bash
cd alphapilot/modules/portal/web
npm install
npm run build
cd ../../../../
```

### 3. Configure Environment Variables

```bash
cp .env.example .env
```

At minimum, fill in:

```env
OPENAI_API_KEY=<your_api_key>
OPENAI_BASE_URL=<your_api_base_url>
CHAT_MODEL=<your_chat_model>
REASONING_MODEL=<your_reasoning_model>
```

API key notes:

- `OPENAI_API_KEY` should be filled with the key issued by the model provider you actually use.
- If you use the official OpenAI API, `OPENAI_BASE_URL` should usually be `https://api.openai.com/v1`.
- If you use an OpenAI-compatible provider such as Azure OpenAI or another compatible gateway, replace both `OPENAI_BASE_URL` and `OPENAI_API_KEY` with the values from that same platform; do not mix a key from one provider with the base URL of another.
- `CHAT_MODEL` and `REASONING_MODEL` must be model IDs that are available under the `OPENAI_BASE_URL` you configured.
- Keep the values as plain strings in `.env`; do not commit real keys to the repository.

### 4. Prepare Data

```bash
alphapilot prepare_data download \
  --stock_csv important_data/stock_lists/main_stock_2026_4_27.csv \
  --adjust_mode backward

alphapilot prepare_data convert \
  --stock_csv important_data/stock_lists/main_stock_2026_4_27.csv \
  --adjust_mode backward \
  --market main_stock_2026_4_27
```

### 5. Start the Portal

```bash
alphapilot portal
```

Default URL: `http://127.0.0.1:19901`

> The default timezone is **Asia/Shanghai**, which affects scheduled tasks and timestamp display. You can change it in the portal's Advanced page under Portal Settings, or run `alphapilot timezone Asia/Shanghai`.

### 6. Run a Task

Start a factor mining task:

```bash
alphapilot mine --direction "behavioral finance hypothesis" --step_n 5
```

Or run a backtest on an existing factor file:

```bash
alphapilot backtest --factor_path /path/to/factors.csv
```

Or snapshot a strategy into a resumable daily-trade session and generate the next rebalance plan:

```bash
alphapilot trade_session_create --strategy_name "<strategy name>" --name demo_session --init_cash 500000
alphapilot daily_signals --session demo_session
```

Or create and replay a formal technical-indicator timing instance:

```bash
alphapilot trading_instance_create \
  --instance_id=ma_5_20 --strategy_id=dual_ma --universe=sh.600000 \
  --params='{"short_window":5,"long_window":20}' --frequency=day \
  --portfolio_policy='{"policy_id":"timing_fixed_exposure","params":{"target_percent":0.2}}'
alphapilot trading_instance_validate --instance_id=ma_5_20
alphapilot trading_backtest --instance_id=ma_5_20 \
  --options='{"data_dir":"./data","adjust_mode":"none"}' --wait=True \
  --output_dir=./results/ma_5_20
```

### 7. Rehearse in Paper Mode, Then Add Live Trading (Optional)

First inspect the available modes and installed broker/quote plugins:

```bash
alphapilot live_modes
alphapilot live_plugins
alphapilot live_brokers
alphapilot live_quote_providers
```

The built-in paper broker works without a real broker plugin. After installing an XTP Pro or EMT plugin, run a preflight that neither logs in nor places an order before attempting a connection:

```bash
alphapilot live_preflight --broker xtp --network=False
alphapilot live_connect --mode live --broker xtp --timeout 30
```

Broker SDK bindings and adapters are not synchronized with the core repository; install them from an authorized private index or local wheelhouse. Keep real credentials only in a local `.env` or the deployment environment. See the [live setup guide](docs/live-xtp.md) for the complete procedure.

## 🧭 Typical Workflow

1. Use `prepare_data` to prepare market data and Qlib data; factor h5 cache is generated automatically by backtest/mining tasks.
2. Use `mine` or AlphaForge commands to generate candidate factors.
3. Use `backtest` for portfolio backtests or quick IC screening, then review results in the portal.
4. Save effective strategies as strategy assets, then continue validation with `strategy_backtest`, `daily_signals`, or a resumable `trade_session`.
5. To move toward execution, rehearse in the order `dry_run → paper → live`; only attach a target portfolio or timing strategy to the daemon after preflight, risk, and recovery checks pass.

## 📚 More Documentation

- [Full CLI command reference](docs/alphapilot-cli.md)
- [Project structure and architecture](docs/alphapilot-structure.md)
- [Docker deployment and service mode](docs/DOCKER.md)
- [Docker run notes and troubleshooting](docs/DOCKER-RUN.md)
- [XTP Pro / EMT live-trading setup](docs/live-xtp.md)
- [Live broker/quote pip plugin guide](docs/live-plugins.md)
- [important_data directory, templates, and assets](important_data/README.md)
- [AlphaForge notes](alphapilot/modules/alphaforge/README.md)

## 📂 Directory Structure

```text
AlphaPilot/
├── alphapilot/          # Core, research systems, Live Runtime, and Portal
├── important_data/      # Factor library, strategy assets, templates, and stock pools
├── docs/                # CLI, Docker, architecture, and live setup guides
├── scripts/             # Data maintenance plus live preflight / smoke tools
├── tests/               # Offline, integration, and live contract tests
├── Dockerfile.live      # x86_64 broker SDK runtime image
├── docker-compose.yml   # Docker service orchestration
└── README.md            # Chinese project home page
```

XTP Pro / EMT SDK bindings and broker plugins are optional, potentially license-restricted packages rather than part of the AlphaPilot core source. Local plugin directories used during development are excluded from Git synchronization by default.

## 🚧 Development Status and Roadmap

> AlphaPilot is still under active development. Some known bugs are being fixed and optimized, features and interfaces may change, and the project will continue to be updated.

Planned directions:

- [ ] Add support for US equities
- [ ] Continue expanding quant timing strategies, minute-level research, and stock-selection strategy optimization
- [ ] Improve the interactive UI and add more tunable options, including rebalancing methods and LightGBM model parameters
- [ ] Integrate more factor mining methods
- [ ] Continue fixing known issues and improving documentation and stability
- [ ] Continue improving the paper/live trading system, broker adapters, recovery flow, and safety controls

Issues and PRs are welcome.

For questions or development discussions, you can also contact us by email: ruiwong@zju.edu.cn

## Development Log

| Date | Type | Feature / Module | Goal | Key Changes | Affected Entry | Verification | Status / Follow-up |
|------|------|------------------|------|-------------|----------------|--------------|--------------------|
| 2026-07-10 | Refactor | Live broker/quote plugins | Decouple broker SDKs from the AlphaPilot core and enable discoverable, installable, and removable live-trading integrations | refactored XTP Pro and EMT as `alphapilot.live.plugins` entry-point plugins with independently configurable trading and quote channels; added plugin discovery, availability checks, and `live_plugins` | `alphapilot live_plugins` / `live_brokers` / `live_quote_providers`; Portal Live page; `docs/live-xtp.md`; `docs/live-plugins.md` | Added plugin installation and registry tests, plus CLI, Portal, and gateway regression coverage | Still under development; continue validating connectivity, recovery, and live safety controls in broker environments |
| 2026-07-07 | New | Paper/live trading system | Provide a unified execution layer from research signals to paper/live trading, while keeping real-money workflows behind explicit risk controls | Added the `live` system/module with paper, dry-run, and live modes; added a broker registry and live adapters; added runtime preflight/connect/state, manual orders, target-portfolio submission, daemon lifecycle controls, strategy start/pause/resume/stop, risk status, append-only ledger events, recovery helpers, and Portal Live APIs/UI integration | `alphapilot live_*` CLI; Portal Live page; `/api/live/*`; `docs/live-xtp.md`; `Dockerfile.live` | Added and expanded live engine/runtime/risk/registry/strategy-runner/daemon/recovery/event tests plus Portal live API/frontend coverage | Still under development; continue validating broker-side behavior, production recovery, account safety limits, and live-market operations |
| 2026-07-01 | New | Quant timing system | Add reusable technical-indicator timing on top of the existing stock-selection/backtest workflow, with an execution boundary reserved for later paper/live trading | Added the `timing` system/module and `alphapilot timing_strategies` / `timing_signal` / `timing_backtest` CLI commands; implemented built-in BOLL, SMA, dual MA, RSI, KDJ, Aroon, StochRSI, and ARBR strategies; added pandas technical indicators and signal helpers, a long/cash backtest engine, next-bar-open fills, fee/slippage/lot-size constraints, and signals/trades/equity_curve/positions/summary artifacts; added the Portal Timing page, APIs, background job support, and result previews; added downloaded-symbol picking for stock-pool create/add flows; fixed frontend `ignoreDeprecations` for TypeScript 5.x typecheck | `alphapilot timing_*` CLI; Portal Timing page; `/api/timing/*`; `timing_backtest` background job; Portal Market Data stock-pool manager | Added `tests/test_timing_indicators.py`, `tests/test_timing_engine.py`, and `tests/test_timing_system.py`; expanded CLI, Portal API/job, frontend page, and parameter-spec tests | Base version completed; next steps are broader minute-level timing, portfolio-level capital allocation, and live-trading adapters |
| 2026-06-30 | New | Minute-level data workflow | Extend AlphaPilot beyond daily data so intraday research can run through the same workflow | Added support for minute-level market data download, visualization, factor mining, and backtesting | Market data download; Portal data visualization; factor mining; factor backtest | Pending full regression | Completed |
| 2026-06-29 | New | Stock pool management | Let users batch-organize stocks into named pools reusable across backtest and factor mining | Added a `stock_pool` CLI module with full CRUD (`pool_create` / `pool_list` / `pool_add` / `pool_remove` / `pool_rename` / `pool_delete`, etc.); pools stored as JSON source-of-truth and synced to Qlib instruments; Portal Market Data page gained a stock-pool management section, and mining / backtest / library / scheduler forms now turn the market / instruments field into a stock-pool dropdown | `alphapilot pool_*` CLI; Portal Market Data page; Mining / Backtest / Library / Scheduler forms; `/api/data/instrument-sets`; `/api/modules/run` | `pytest tests/test_stock_pool.py tests/test_kernel_registry.py`; `npm run build`; `npm run test` | Completed |
| 2026-06-26 | New | Portal parameter help | Make complex task/config panels easier to use consistently | Added reusable question-mark help panel, expanded help copy across mining, backtest, library, market data, daily trade, scheduler, notifications, and advanced settings; added Daily Trade left-panel title | Portal task/config panels | `npm run build`; `npm run typecheck` blocked by existing `tsconfig.json` `ignoreDeprecations: "6.0"` incompatibility with TypeScript 5.9 | Completed; fix tsconfig before relying on typecheck |
| 2026-06-24 | Optimization | Portal market data / K-line chart | Improve the local K-line viewing experience | Main chart plus sub-chart layout; sub-chart supports amount, volume, turnover, and price-change switching; added range buttons, unified hover behavior, and light/dark theme adaptation | Portal Market Data page | `npm run typecheck`; `npm run build` | Completed |
| 2026-06-24 | New | Factor library / duplicate check | Help clean duplicate or near-duplicate factors and reduce factor library maintenance cost | Added duplicate factor detection, keep/delete suggestions, bulk-delete APIs, and Portal entry | Portal Factor / Strategy Library page; `/api/factors/duplicates`; `/api/factors/bulk-delete` | Frontend `npm run typecheck`; `npm run build` covered UI compilation | Completed |

> [!WARNING]
> The live-trading solution is still being validated. Please do not log in with your personal live-trading account.

## 🙏 Acknowledgements

This project is inspired by [RndmVariableQ/AlphaAgent](https://github.com/RndmVariableQ/AlphaAgent) and [DulyHao/AlphaForge](https://github.com/DulyHao/AlphaForge), with further development and optimization. Thanks to the original authors and the community.

<div align="center">
<br>
<img src="docs/logo.svg" alt="AlphaPilot" width="72" align="middle">
&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<b>×</b>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;
<img src="docs/zju_eagle_lab.svg" alt="ZJU Eagle Lab" width="72" align="middle">
<br><br>
<sub><b>AlphaPilot · Stock Quantitative Research Platform</b>&nbsp;&nbsp;×&nbsp;&nbsp;<b>ZJU Eagle Lab</b></sub>
</div>
