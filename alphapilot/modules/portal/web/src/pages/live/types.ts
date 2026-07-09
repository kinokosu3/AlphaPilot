export type LiveConfigSnapshot = {
  mode: string;
  broker: string;
  timezone: string;
  ledger_dir: string;
  state_dir: string;
  risk: Record<string, number>;
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
};

export type LiveTrade = {
  [key: string]: unknown;
  trade_id: string;
  code: string;
  side: string;
  price: number;
  volume: number;
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
};

export type LiveState = {
  snapshot: LiveEngineSnapshot;
  account: { buying_power: number; balance: number; available?: number };
  positions: LivePosition[];
  orders: LiveOrder[];
  trades: LiveTrade[];
  ledger: Array<{ ts: string; kind: string }>;
  runtime?: { mode?: string; broker?: string; state_dir?: string; ledger_dir?: string };
};

export type LiveStatus = {
  config: LiveConfigSnapshot;
  modes: string[];
  running: boolean;
  state?: LiveState;
};

export type LiveRuntimeSnapshot = {
  config: { mode: string; broker: string; ledger_dir: string; state_dir: string };
  engine: LiveEngineSnapshot;
  account: { account_id?: string; balance: number; available: number; frozen?: number; buying_power?: number; gateway?: string } | null;
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
  description?: string;
  gateway_importable: boolean;
  missing_env: string[];
  network_checked: boolean;
  endpoints: Array<{ name: string; host: string; port: number; ok: boolean; detail: string }>;
  ok: boolean;
};

export type LiveBrokerCapabilities = {
  asset_classes?: string[];
  supports_tick?: boolean;
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
  env_fields: string[];
  missing_env: string[];
  capabilities: LiveBrokerCapabilities;
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
