export type LiveConfigSnapshot = {
  mode: string;
  broker: string;
  trade_broker?: string;
  quote_provider?: string;
  timezone: string;
  ledger_dir: string;
  state_dir: string;
  risk: Record<string, number>;
  market_data?: {
    enabled: boolean;
    data_dir: string;
    retention_days: number;
    snapshot_interval: number;
    stale_after_seconds: number;
  };
};

export type LivePosition = {
  [key: string]: unknown;
  code: string;
  exchange: string;
  volume: number;
  available: number;
  yd_volume: number;
  frozen: number;
  price: number;
  settlement_price?: number;
  margin?: number;
  pnl?: number;
  gateway?: string;
};

export type LiveOrder = {
  [key: string]: unknown;
  order_id: string;
  code: string;
  exchange?: string;
  side: string;
  price: number;
  volume: number;
  traded: number;
  status: string;
  active: boolean;
  type?: string;
  offset?: string;
  reference?: string;
  gateway?: string;
  message?: string;
};

export type LiveTrade = {
  [key: string]: unknown;
  trade_id: string;
  code: string;
  side: string;
  price: number;
  volume: number;
  exchange?: string;
  order_id?: string;
  offset?: string;
  gateway?: string;
};

export type LiveAccount = {
  account_id?: string;
  balance: number;
  available: number;
  frozen?: number;
  buying_power?: number;
  margin?: number;
  commission?: number;
  close_profit?: number;
  position_profit?: number;
  risk_ratio?: number;
  gateway?: string;
};

export type LiveEngineSnapshot = {
  mode: string;
  halted: boolean;
  connection: string;
  session: string;
  buying_power: number;
  active_orders: number;
  positions: number;
  contracts?: number;
  ticks?: number;
  subscribed_symbols?: string[];
};

export type LiveState = {
  snapshot: LiveEngineSnapshot;
  account: LiveAccount;
  positions: LivePosition[];
  orders: LiveOrder[];
  trades: LiveTrade[];
  ledger: Array<{ ts: string; kind: string }>;
  runtime?: { mode?: string; broker?: string; trade_broker?: string; quote_provider?: string; state_dir?: string; ledger_dir?: string };
};

export type LiveStatus = {
  config: LiveConfigSnapshot;
  modes: string[];
  running: boolean;
  state?: LiveState;
};

export type LiveRuntimeSnapshot = {
  config: { mode: string; broker: string; trade_broker?: string; quote_provider?: string; ledger_dir: string; state_dir: string };
  engine: LiveEngineSnapshot;
  account: LiveAccount | null;
  positions: LivePosition[];
  orders: LiveOrder[];
  trades: LiveTrade[];
  ledger_tail?: Array<{ ts: string; kind: string }>;
};

export type LiveRuntimeState = {
  exists: boolean;
  state_path: string;
  state?: LiveRuntimeSnapshot;
  config?: LiveRuntimeSnapshot["config"];
};

export type LivePreflight = {
  broker: string;
  trade_broker?: string;
  quote_provider?: string;
  description?: string;
  gateway_importable: boolean;
  missing_env: string[];
  network_checked: boolean;
  endpoints: Array<{ name: string; host: string; port: number; ok: boolean; detail: string }>;
  ok: boolean;
  trade?: LivePreflightChannel;
  quote?: LivePreflightChannel;
};

export type LivePreflightChannel = {
  name: string;
  broker: string;
  description?: string;
  gateway?: string;
  gateway_importable: boolean;
  missing_env: string[];
  network_checked: boolean;
  endpoints: Array<{ name: string; host: string; port: number; ok: boolean; detail: string }>;
  ok: boolean;
};

export type LiveBrokerCapabilities = {
  asset_classes?: string[];
  exchanges?: string[];
  supports_tick?: boolean;
  supports_depth?: boolean;
  supports_contract_query?: boolean;
  supports_account_query?: boolean;
  supports_position_query?: boolean;
  supports_order_query?: boolean;
  supports_trade_query?: boolean;
  supports_cancel?: boolean;
  supports_margin?: boolean;
  supports_history?: boolean;
};

export type LiveBrokerSpec = {
  name: string;
  description: string;
  gateway: string;
  gateway_importable: boolean;
  availability_detail?: string;
  plugin_id?: string;
  distribution?: string;
  version?: string;
  roles?: string[];
  env_fields: string[];
  missing_env: string[];
  capabilities: LiveBrokerCapabilities;
};

export type LiveQuoteProviderSpec = LiveBrokerSpec;

export type LivePluginDiagnostics = {
  api_version: number;
  entry_point_group: string;
  plugins: Array<{
    plugin_id: string;
    distribution?: string;
    version?: string;
    status: string;
    error?: string;
    providers: Array<{ name: string; roles: string[] }>;
  }>;
  issues: Array<{
    plugin_id: string;
    kind: string;
    error: string;
    distribution?: string;
    version?: string;
  }>;
};

export type LiveMarketTick = {
  [key: string]: unknown;
  key: string;
  code: string;
  exchange: string;
  name?: string;
  last_price: number;
  pre_close: number;
  change: number;
  change_pct: number;
  bid_price_1: number;
  ask_price_1: number;
  bid_volume_1: number;
  ask_volume_1: number;
  volume: number;
  turnover: number;
  datetime?: string | null;
  received_at?: string | null;
  age_seconds?: number | null;
  stale: boolean;
  gateway?: string;
};

export type LiveMarketRecorder = {
  enabled: boolean;
  healthy: boolean;
  degraded: boolean;
  queue_depth: number;
  written_ticks: number;
  written_bars: number;
  dropped_ticks: number;
  dropped_bars: number;
  last_error?: string | null;
  last_flush_at?: string | null;
};

export type LiveMarketSnapshot = {
  exists: boolean;
  generated_at?: string;
  quote_provider?: string;
  daemon_running?: boolean;
  daemon_status?: string;
  subscribed_symbols: string[];
  stale_after_seconds: number;
  ticks: LiveMarketTick[];
  recorder?: LiveMarketRecorder;
};

export type LiveMarketBars = {
  symbol: string;
  label?: string;
  interval: number;
  date_range: string[];
  rows: Array<Record<string, unknown>>;
};

export type LiveConnectResult = { ready: boolean; state: LiveRuntimeSnapshot };

export type LiveDaemonStatus = {
  exists: boolean;
  path: string;
  alive?: boolean;
  starting?: boolean;
  running: boolean;
  status?: string;
  pid?: number;
  ready?: boolean;
  mode?: string;
  broker?: string;
  trade_broker?: string;
  quote_provider?: string;
  commands_processed?: number;
  runner?: { enabled?: boolean; strategy?: string; freq?: string };
  runner_status?: LiveRunnerStatus | null;
  last_command?: {
    [key: string]: unknown;
    id?: string;
    action?: string;
    ok?: boolean;
    error?: string;
    message?: string;
    order_id?: string;
  };
  command_status_tail?: LiveCommandStatus[];
  state_path?: string;
  log_path?: string;
  state?: LiveRuntimeSnapshot;
};

export type LiveDaemonStopResult = LiveDaemonStatus & { stopped: boolean };

export type LiveDaemonCommandResult = {
  accepted: boolean;
  command?: { id: string; action: string };
  daemon?: LiveDaemonStatus;
  reason?: string;
};

export type LiveRunnerStatus = {
  [key: string]: unknown;
  enabled?: boolean;
  active?: boolean;
  paused?: boolean;
  stopped?: boolean;
  started?: boolean;
  freq?: string;
  symbols?: string[];
  pending_requests?: number;
  algo_armed?: boolean;
  last_session?: string | null;
  config?: { strategy?: string; freq?: string; enabled?: boolean; params?: Record<string, unknown> };
};

export type LiveCommandStatus = {
  [key: string]: unknown;
  ts?: string;
  id?: string;
  action?: string;
  stage?: string;
  result?: { ok?: boolean; message?: string; error?: string; action?: string };
};

export type LiveLedgerEvent = {
  [key: string]: unknown;
  ts: string;
  kind: string;
  source?: string;
  order_id?: string;
  reference?: string;
  command_id?: string;
  payload?: Record<string, unknown>;
};

export type LiveRiskStatus = {
  exists: boolean;
  state_path: string;
  ledger_dir: string;
  risk?: {
    limits?: Record<string, number>;
    enforce_session?: boolean;
    orders_today?: number;
    value_today?: number;
    seen_refs?: string[];
  } | null;
  recovery?: {
    risk_restored?: boolean;
    warnings?: Array<{ kind?: string; detail?: string; order_ids?: string[] }>;
    reconciliation?: Record<string, unknown>;
  } | null;
  recent_rejections?: LiveLedgerEvent[];
};

export type LiveLedgerEvents = { count: number; events: LiveLedgerEvent[] };

export type AsyncResource<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => Promise<void>;
};

export type JsonInputState = {
  raw: string;
  setRaw: (value: string) => void;
  parse: () => Record<string, unknown>;
};
