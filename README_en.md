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

[Quick Start](#-quick-start)&nbsp;·&nbsp;[Custom Strategy](#-custom-strategy-tutorial)&nbsp;·&nbsp;[Core Features](#core-features)&nbsp;·&nbsp;[Typical Workflow](#-typical-workflow)&nbsp;·&nbsp;[Docs](#-more-documentation)&nbsp;·&nbsp;[Docker Deployment](docs/DOCKER.md)

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
| Custom signal strategies | `strategies/*/strategy.toml` | Extend signal logic with a local Python strategy and explicit manifest, then preview, replay, and deploy it through the shared runtime |
| Quant timing | `alphapilot trading_instance_create` / `trading_backtest` | Formal strategy instances, unified replay, and controlled deployment; legacy `timing_*` commands were removed in 0.2.0 |
| Paper / live trading | `alphapilot live_*` | `dry_run` / `paper` / `simulation` / `shadow` / `live` modes, unified risk and OMS, daemon control, recovery reconciliation, and an audit ledger; XTP Pro, EMT, and OpenCTP TTS connect through optional plugins |
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
  <img src="docs/assets/portal/mining.png" alt="Factor mining page: LLM factor mining and formula-based mining" width="860">
  <br><br>
  <img src="docs/assets/portal/library.png" alt="Factor / strategy library: unified factor asset management" width="860">
</div>

### Backtesting and Evaluation

The project includes multiple backtesting and evaluation modes. It supports formal portfolio backtests and quick screening for large candidate factor sets. The README keeps only the common entries; see the [CLI command reference](docs/alphapilot-cli.md) for more commands and parameters.

- `multi_combined`: combine multiple factors, train a model, and run a portfolio backtest
- `single_ic`: quickly calculate IC, RankIC, and ICIR for each factor
- `multi_sequential`: run full portfolio backtests for factors one by one
- Portal backtest page: visualizes returns, excess returns, account composition, turnover, daily details, factor leaderboards, and benchmark comparisons

Main entry: `alphapilot backtest --factor_path /path/to/factors.csv`

<div align="center">
  <img src="docs/assets/portal/backtest.png" alt="Backtesting: cumulative returns, excess returns, and account composition" width="860">
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

The live subsystem connects target portfolios or timing signals to one execution path: `LiveRuntime → LiveEngine → RiskGate → BrokerGateway`. The low-level `dry_run` mode never routes orders; formal strategy deployments use `paper`, `simulation`, `shadow`, or `live`. SHADOW consumes real account and quote data but can never route an order, while LIVE routing remains disabled unless its environment switch is enabled.

- Five runtime modes—`dry_run`, `paper`, `simulation`, `shadow`, and `live`—with foreground and persistent daemon operation
- Manual orders, cancellation, and target-portfolio submission; automated strategies start only through persistent instances and `trading_*` deployment controls
- A single pre-trade gate for session, board-lot, price, cash, position, concentration, per-order, and daily-turnover checks
- OMS state, append-only audit events, runtime snapshots, and recovery reconciliation; a trade-channel disconnect halts execution and requires review before resuming
- XTP Pro, EMT, and OpenCTP TTS broker/quote integrations are decoupled from the core as installable pip plugins; trading and quote providers can be configured independently
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
- The Live page separates paper, broker-simulation, SHADOW, and LIVE workspaces and exposes preflight, daemon, deployment, diagnostics, risk, and audit controls
- Suitable for both local research environments and server deployments

Main entry: `alphapilot portal`

<div align="center">
  <img src="docs/assets/portal/home.png" alt="Portal home page and task panel" width="860">
</div>

### Data Preparation and Management

The project includes a complete A-share data preparation pipeline, from raw market data to Qlib data. Factor h5 cache is generated automatically by research and backtest tasks. This README keeps the shortest path; see the detailed documentation for data sources, adjustment modes, and advanced parameters.

- Supports baostock and tushare data sources
- Supports market data downloads, price adjustment, and Qlib conversion
- Supports stock pool management and single-stock data maintenance
- Connects directly with factor mining, backtesting, and daily signal generation

Main entry: `alphapilot prepare_data download --stock_csv important_data/stock_lists/main_stock_2026_4_27.csv`

<div align="center">
  <img src="docs/assets/portal/market.png" alt="Market data: data actions, stock pools, and single-stock management" width="860">
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
  <img src="docs/assets/portal/notifications.png" alt="Notification settings and command receiver" width="860">
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
  --instance_id=ma_5_20 --strategy_id=dual_ma --universe=600000.SSE \
  --params='{"short_window":5,"long_window":20}' --frequency=day \
  --data_policy='{"feature_adjustment":"backward","history_window":21}' \
  --portfolio_policy='{"policy_id":"timing_fixed_exposure","params":{"target_percent":0.2}}'
alphapilot trading_instance_validate --instance_id=ma_5_20
alphapilot trading_backtest --instance_id=ma_5_20 \
  --wait=True --output_dir=./results/ma_5_20
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

## 🧩 Custom Strategy Tutorial

AlphaPilot uses “strategy” for two related concepts. `strategy_create` produces a **research strategy asset** containing factors, a model, and backtest settings. This tutorial creates a **strategy definition** that supplies signals to the shared trading runtime. The Provider v1 interface below is the shortest route from Python code to a validated preview and replay; AlphaPilot automatically adapts it to the formal runtime.

### 1. Create the strategy directory

Create a one-level strategy directory at the repository root. Discovery scans `strategies/<strategy ID>/strategy.toml`; it does not recursively import arbitrary Python files:

```text
strategies/close_above_sma/
├── strategy.py
└── strategy.toml
```

Save this as `strategies/close_above_sma/strategy.py`:

```python
from __future__ import annotations

import pandas as pd

from alphapilot.systems.timing.base import TimingContext
from alphapilot.systems.timing.strategies import RuleTimingStrategy


class CloseAboveSMA(RuleTimingStrategy):
    """Stay long while the close is above its moving average."""

    name = "close_above_sma"
    defaults = {"window": 20}

    def _instrument_signal(
        self,
        bars: pd.DataFrame,
        context: TimingContext,
    ) -> pd.DataFrame:
        del context
        close = pd.to_numeric(bars["close"], errors="coerce")
        average = close.rolling(int(self.params["window"])).mean()
        signal = (close > average).fillna(False).astype(int)
        score = (close / average - 1).fillna(0.0)
        return self._frame(bars, signal, score, "close_above_sma")
```

`RuleTimingStrategy` groups rows by `instrument`, while `_frame` creates the runtime columns `datetime`, `instrument`, `signal`, `target_percent`, `score`, and `reason`. Input `bars` must contain at least `datetime`, `instrument`, `open`, `high`, `low`, and `close`; strategies that need trading activity can also read `volume` and `amount`. Here, `signal=1` means long and `signal=0` means flat. A strategy only generates signals—it must not access a broker or submit orders directly.

### 2. Declare the strategy manifest

Save this as `strategies/close_above_sma/strategy.toml`:

```toml
[strategy]
id = "close_above_sma"
version = "1.0.0"
kind = "rule"
factory = "strategy:CloseAboveSMA"
api_version = 1
provider_api_version = 1
signal_kind = "instrument_timing"
supported_assets = ["equity", "fund"]
supported_frequencies = ["day"]
required_history = 21
state_schema_version = 1
supported_run_modes = ["paper", "simulation", "shadow", "live"]
description = "Long when close is above its simple moving average."
parameter_schema_json = '''
{"type":"object","properties":{"window":{"type":"integer","default":20,"minimum":2}},"required":["window"],"additionalProperties":false}
'''
```

`factory` uses the `module filename:class name` format. `parameter_schema_json` supplies parameter defaults and validation rules. `required_history` is the default warm-up length; for parameter names containing `window`, the runtime also raises the requirement from the actual instance value—for example, `window=60` requires at least 61 bars.

Adapting the example to your own algorithm normally takes three changes: replace the indicator and entry/exit conditions in `_instrument_signal`; add every tunable value to both `defaults` and `parameter_schema_json`; and make `required_history` cover the longest indicator lookback while updating `supported_frequencies` to match the data you consume.

### 3. Verify discovery

Run this from the repository root:

```bash
alphapilot trading_definitions
```

`close_above_sma` should appear under `definitions`. If it appears under `quarantined`, use its `reason` to check the TOML, import path, duplicate ID, or API version. To store strategies outside the repository, set `ALPHAPILOT_STRATEGY_DIR=/absolute/path/to/strategies`. If the Portal is already running, restart it after changing strategy code or manifests so discovery runs again.

### 4. Create, preview, and replay an instance

First prepare market data as described in [Quick Start](#-quick-start). Then bind the definition to concrete parameters, a universe, a data policy, and a portfolio policy:

```bash
alphapilot trading_instance_create \
  --instance_id=sma_20_demo \
  --strategy_id=close_above_sma \
  --universe=600000.SSE \
  --params='{"window":20}' \
  --frequency=day \
  --data_policy='{"feature_adjustment":"backward","history_window":21,"data_version":"daily-bars-2026-07"}' \
  --portfolio_policy='{"policy_id":"timing_fixed_exposure","params":{"target_percent":0.2,"cash_buffer":0.1,"max_position_weight":0.3}}'

alphapilot trading_instance_validate --instance_id=sma_20_demo
alphapilot trading_preview --instance_id=sma_20_demo \
  --output_path=./results/sma_20_preview.json
alphapilot trading_backtest --instance_id=sma_20_demo \
  --wait=True --output_dir=./results/sma_20_replay
```

`target_percent` belongs to the PortfolioPolicy, not the signal algorithm: it controls the target weight of each instrument while its signal is active. You can therefore reuse one signal definition across instances with different exposure, cash-buffer, and position-limit settings. The replay directory contains signals, target weights, orders, fills, positions, equity, and summary artifacts.

### 5. Build multi-factor or model-based strategies

Keep data loading, factor/model inference, portfolio construction, and order routing in separate layers. A Provider computes factors and emits a `SignalEnvelope`; immutable research artifacts bind model and factor versions; a PortfolioPolicy converts signals to target weights; AlphaPilot retains ownership of AccountSizer, RiskGate, and OMS. Use Provider v2—and implement `initialize`, `warmup`, `evaluate`, `snapshot`, `restore`, and `stop`—when the algorithm needs cross-session state, online-model state, or recovery.

Snapshot an existing factor/model research asset into an instance as follows:

```bash
alphapilot trading_instance_from_research \
  --instance_id=lgb_factor_v3 \
  --strategy_name=my_lgb_factor_asset \
  --universe=600000.SSE,000001.SZ,510300.SSE \
  --portfolio_policy='{"policy_id":"selection_topk_dropout_equal_weight","params":{"topk":10,"n_drop":2,"max_position_weight":0.1}}'
alphapilot trading_instance_validate --instance_id=lgb_factor_v3
```

The snapshot binds model SHA-256, factor/data fingerprints, universe, and policy. Prefer separate instance IDs for different model or factor sets; never let a running strategy load an arbitrary model path. If parameters, model, factors, universe, data policy, or PortfolioPolicy change, the existing deployment is retained but becomes `stale`. Stop the daemon, validate the instance again, and call `trading_deploy` once more to bind the new `config_hash`.

### 6. Configure PAPER, simulation, SHADOW, or LIVE independently

REPLAY is a historical `trading_backtest` operation, not a deployment level. Any validated instance can be assigned or reassigned directly to a supported run mode while its daemon is stopped:

```bash
# Built-in local matching; Providers are forced to paper
alphapilot trading_deploy --instance_id=sma_20_demo --run_mode=paper

# Broker simulation; the trade Provider must advertise a simulation account
alphapilot trading_deploy --instance_id=sma_20_demo --run_mode=simulation \
  --trade_provider=tts --quote_provider=emt --account_profile=tts-sim-main

# Read-only real-account shadow; it can never route orders
alphapilot trading_deploy --instance_id=sma_20_demo --run_mode=shadow \
  --trade_provider=xtp --quote_provider=xtp --account_id=YOUR_ACCOUNT_ID

# Configure LIVE directly; no PAPER/SHADOW/UAT/comparison record is required
ALPHAPILOT_AUTOMATED_LIVE_ENABLED=true \
alphapilot trading_deploy --instance_id=sma_20_demo --run_mode=live \
  --trade_provider=xtp --quote_provider=xtp --account_id=YOUR_ACCOUNT_ID

alphapilot trading_start --instance_id=sma_20_demo
alphapilot trading_deployments
alphapilot trading_diagnostics --instance_id=sma_20_demo
```

A LIVE start remains paused pending reconciliation. Run `trading_reconcile`, then explicitly call `trading_resume`. Removing promotion gates does not remove order safety: LIVE still checks the environment switch, account/Provider binding, single writer, market/contract metadata, reconciliation, heartbeat, Kill Switch, and per-order RiskGate. PAPER/SHADOW sessions and `trading_decision_compare` are diagnostics only and never grant deployment authority.

Deployment configuration and lifecycle HTTP endpoints are available only on a loopback-bound Portal and do not require an Operator Bearer or mandatory reason. Generate an operator token locally with `alphapilot trading_operator_token --operator_id=...`; its plaintext is returned once and is now needed only for protected Kill Switch, Broker UAT, strategy-write, and manual-trading operations.

When iterating, bump the manifest `version` and restart long-running processes. Code, version, or any instance binding change produces a new `config_hash` and requires validation plus deployment rebinding, while retaining the old deployment record and diagnostics.

For a more complete local example with volume confirmation, see [`strategies/dual_ma_volume_confirmed`](strategies/dual_ma_volume_confirmed). Implement Provider v2 when you need explicit lifecycle state, snapshots, and recovery. The [strategy extension guide](docs/developer/strategy-extension.md) covers Provider v2, pip entry points, and custom PortfolioPolicy implementations; the [strategy instance guide](docs/user/strategy-instances.md) covers lifecycle and troubleshooting.

## 🧭 Typical Workflow

1. Use `prepare_data` to prepare market data and Qlib data; factor h5 cache is generated automatically by backtest/mining tasks.
2. Use `mine` or AlphaForge commands to generate candidate factors.
3. Use `backtest` for portfolio backtests or quick IC screening, then review results in the portal.
4. Save effective strategies as strategy assets, then continue validation with `strategy_backtest`, `daily_signals`, or a resumable `trade_session`.
5. To move toward execution, validate the instance and independently select `paper | simulation | shadow | live`. `dry_run` remains a low-level debug mode and REPLAY remains a backtest operation. A research team may impose its own rehearsal or comparison thresholds, but those diagnostics neither grant nor block LIVE.

## 📚 More Documentation

The Chinese documentation center is the canonical, code-checked manual for the current `0.2.x` line. This English README remains a project overview and is not maintained as a complete translated copy of every interface.

- [Chinese documentation center: user guides, developer docs, and generated CLI/API references](docs/index.md)
- [Strategy instances, previews, and unified replays (Chinese)](docs/user/strategy-instances.md)
- [Custom strategies, PortfolioPolicy, and artifacts (Chinese)](docs/developer/strategy-extension.md)
- [Docker run notes and troubleshooting](docs/DOCKER-RUN.md)
- [XTP Pro / EMT live-trading setup](docs/live-xtp.md)
- [Live broker/quote pip plugin guide](docs/live-plugins.md)
- [important_data directory, templates, and assets](important_data/README.md)
- [AlphaForge notes](alphapilot/modules/alphaforge/README.md)

## 📂 Directory Structure

```text
AlphaPilot/
├── alphapilot/          # Core, research systems, Live Runtime, and Portal
├── strategies/          # Local custom signal strategies and explicit manifests
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
