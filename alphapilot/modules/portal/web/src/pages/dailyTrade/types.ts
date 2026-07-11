export type TradeSessionManifest = {
  name: string;
  source_strategy?: string;
  current_date?: string | null;
  status?: string;
  init_cash?: number | null;
  market?: string | null;
  n_factors?: number;
};

export type SessionLogRow = {
  date: string;
  n_buy?: number;
  n_sell?: number;
  cash?: number;
  n_positions?: number;
  nav?: number;
  ret?: number;
  cost?: number;
  turnover?: number;
};

export type CashflowRow = {
  ts?: string;
  date?: string;
  delta?: number;
  balance_after?: number;
  note?: string;
};

export type TradeSessionDetail = {
  manifest: TradeSessionManifest;
  state?: { date?: string; cash?: number; positions?: Record<string, number> } | null;
  history?: SessionLogRow[];
  cashflows?: CashflowRow[];
};
