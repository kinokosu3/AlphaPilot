import { useEffect, useMemo, useState } from "react";
import { api, qs } from "../../api";
import { CandlestickPlot } from "../../components/CandlestickPlot";
import { Alert, DataTable, Spinner, StatusPill, Tabs } from "../../components";
import { useSerialPolling } from "../../hooks";
import { useI18n } from "../../i18n";
import type { LiveMarketBars, LiveMarketSnapshot, LiveMarketTick } from "./types";

type Props = {
  mode: string;
  tradeBroker: string;
  quoteProvider: string;
  daemonRunning: boolean;
  deploymentInstanceId?: string;
  embedded?: boolean;
};

type ChartRow = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount: number;
};

function number(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function price(value: unknown): string {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(2) : "-";
}

function compact(value: unknown): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "-";
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 2 }).format(parsed);
}

function timeLabel(value: unknown): string {
  const match = String(value || "").match(/[ T](\d{2}:\d{2}(?::\d{2})?)/);
  return match ? match[1] : "-";
}

function normalizeRows(payload: LiveMarketBars | null): ChartRow[] {
  return (payload?.rows || []).flatMap((row) => {
    const date = String(row.date || "");
    const open = number(row.open);
    const high = number(row.high);
    const low = number(row.low);
    const close = number(row.close);
    if (!date || !open || !high || !low || !close) return [];
    return [{ date, open, high, low, close, volume: number(row.volume), amount: number(row.amount) }];
  });
}

export function LiveMarketPanel({
  mode,
  tradeBroker,
  quoteProvider,
  daemonRunning,
  deploymentInstanceId,
  embedded = false,
}: Props) {
  const { t } = useI18n();
  const query = { mode, broker: tradeBroker, trade_broker: tradeBroker, quote_provider: quoteProvider };
  const deploymentBase = deploymentInstanceId
    ? `/api/trading/deployments/${encodeURIComponent(deploymentInstanceId)}/market`
    : "";
  const snapshot = useSerialPolling(
    () => api.get<LiveMarketSnapshot>(
      deploymentBase
        ? `${deploymentBase}/snapshot`
        : `/api/live/market/snapshot${qs(query)}`,
    ),
    [mode, tradeBroker, quoteProvider, deploymentBase],
    { enabled: daemonRunning, intervalMs: 1000 },
  );
  const [selectedSymbol, setSelectedSymbol] = useState("");
  const [interval, setIntervalSeconds] = useState(60);
  const availableSymbols = useMemo(
    () => daemonRunning
      ? Array.from(new Set([
          ...(snapshot.data?.subscribed_symbols || []),
          ...(snapshot.data?.ticks || []).map((row) => row.key),
        ])).sort()
      : [],
    [daemonRunning, snapshot.data],
  );
  const awaitingFirstTick = useMemo(
    () => new Set(snapshot.data?.awaiting_first_tick || []),
    [snapshot.data?.awaiting_first_tick],
  );
  const marketRows = useMemo(() => {
    const ticks = new Map((snapshot.data?.ticks || []).map((row) => [row.key, row]));
    return availableSymbols.map((symbol): LiveMarketTick => ticks.get(symbol) || {
      key: symbol,
      code: symbol.split(".", 1)[0],
      exchange: symbol.includes(".") ? symbol.split(".").slice(-1)[0] || "" : "",
      last_price: 0,
      pre_close: 0,
      change: 0,
      change_pct: 0,
      bid_price_1: 0,
      ask_price_1: 0,
      bid_volume_1: 0,
      ask_volume_1: 0,
      volume: 0,
      turnover: 0,
      stale: true,
      awaiting_first_tick: true,
    });
  }, [availableSymbols, snapshot.data?.ticks]);

  useEffect(() => {
    if (!availableSymbols.length) {
      setSelectedSymbol("");
      return;
    }
    if (!availableSymbols.includes(selectedSymbol)) setSelectedSymbol(availableSymbols[0]);
  }, [availableSymbols, selectedSymbol]);

  const bars = useSerialPolling(
    () => selectedSymbol
      ? api.get<LiveMarketBars>(
          deploymentBase
            ? `${deploymentBase}/bars${qs({ symbol: selectedSymbol, interval, limit: 300 })}`
            : `/api/live/market/bars${qs({ ...query, symbol: selectedSymbol, interval, limit: 300 })}`,
        )
      : Promise.resolve({ symbol: "", interval, date_range: [], rows: [] }),
    [mode, tradeBroker, quoteProvider, deploymentBase, selectedSymbol, interval],
    { enabled: daemonRunning && Boolean(selectedSymbol), intervalMs: 1000 },
  );
  const chartRows = useMemo(() => normalizeRows(bars.data), [bars.data]);
  const chartColors = chartRows.map((row) => row.close >= row.open ? "rgba(239, 83, 80, 0.72)" : "rgba(38, 166, 154, 0.72)");
  const chartHover = chartRows.map((row) => [
    `<b>${row.date.replace("T", " ")}</b>`,
    `${t("klineOpen")}: ${price(row.open)}`,
    `${t("klineHigh")}: ${price(row.high)}`,
    `${t("klineLow")}: ${price(row.low)}`,
    `${t("klineClose")}: ${price(row.close)}`,
    `${t("klineMetricVolume")}: ${compact(row.volume)}`,
    `${t("klineMetricAmount")}: ${compact(row.amount)}`,
  ].join("<br>"));
  const tickCount = Math.min(7, chartRows.length);
  const tickIndexes: number[] = chartRows.length
    ? Array.from(new Set(Array.from({ length: tickCount }, (_, index) =>
        Math.round(index * (chartRows.length - 1) / Math.max(tickCount - 1, 1)))))
    : [];
  const recorder = snapshot.data?.recorder;

  const columns = [
    {
      key: "key", label: t("liveMarketSymbol"),
      render: (row: LiveMarketTick) => (
        <button
          type="button"
          className={`live-symbol-button ${selectedSymbol === row.key ? "active" : ""}`}
          onClick={() => setSelectedSymbol(row.key)}
        >
          {row.name ? `${row.name} ${row.key}` : row.key}
        </button>
      ),
    },
    { key: "last_price", label: t("liveMarketLast"), align: "right" as const, render: (row: LiveMarketTick) => <strong>{row.awaiting_first_tick ? "—" : price(row.last_price)}</strong> },
    {
      key: "change_pct", label: t("liveMarketChange"), align: "right" as const,
      render: (row: LiveMarketTick) => row.awaiting_first_tick
        ? "—"
        : <span className={row.change_pct >= 0 ? "market-up" : "market-down"}>{row.change_pct >= 0 ? "+" : ""}{row.change_pct.toFixed(2)}%</span>,
    },
    { key: "bid_price_1", label: t("liveMarketBid"), align: "right" as const, render: (row: LiveMarketTick) => row.awaiting_first_tick ? "—" : price(row.bid_price_1) },
    { key: "ask_price_1", label: t("liveMarketAsk"), align: "right" as const, render: (row: LiveMarketTick) => row.awaiting_first_tick ? "—" : price(row.ask_price_1) },
    { key: "volume", label: t("klineMetricVolume"), align: "right" as const, render: (row: LiveMarketTick) => row.awaiting_first_tick ? "—" : compact(row.volume) },
    { key: "turnover", label: t("klineMetricAmount"), align: "right" as const, render: (row: LiveMarketTick) => row.awaiting_first_tick ? "—" : compact(row.turnover) },
    { key: "datetime", label: t("liveMarketTime"), render: (row: LiveMarketTick) => timeLabel(row.datetime) },
    {
      key: "stale", label: t("status"),
      render: (row: LiveMarketTick) => (
        <StatusPill status={row.awaiting_first_tick || awaitingFirstTick.has(row.key) ? "waiting" : row.stale ? "stale" : "live"} />
      ),
    },
  ];

  return (
    <section className={`${embedded ? "" : "panel "}live-market-panel`}>
      <div className="panel-head">
        <div>
          <h3>{t("liveMarket")}</h3>
          <span className="muted">{snapshot.data?.quote_provider || quoteProvider || "-"}</span>
        </div>
        <StatusPill status={snapshot.data?.daemon_running || daemonRunning ? "running" : "stopped"} />
      </div>
      {snapshot.error ? <Alert tone="error">{snapshot.error}</Alert> : null}
      {recorder?.last_error ? <Alert tone="error">{recorder.last_error}</Alert> : null}
      <div className="metric-grid compact live-recorder-metrics">
        <div className="metric"><span className="metric-label">{t("liveRecorder")}</span><StatusPill status={recorder?.degraded ? "degraded" : recorder?.enabled ? "healthy" : "disabled"} /></div>
        <div className="metric"><span className="metric-label">{t("liveRecordedTicks")}</span><strong>{compact(recorder?.written_ticks)}</strong></div>
        <div className="metric"><span className="metric-label">{t("liveRecorderQueue")}</span><strong>{recorder?.queue_depth ?? 0}</strong></div>
        <div className="metric"><span className="metric-label">{t("liveDroppedTicks")}</span><strong>{recorder?.dropped_ticks ?? 0}</strong></div>
      </div>
      <div className="live-subscription-groups">
        <div>
          <span>{t("liveStrategySubscriptions")}</span>
          <div className="live-subscription-list">
            {(daemonRunning ? snapshot.data?.strategy_symbols || [] : []).map((symbol) => (
              <button type="button" key={symbol} onClick={() => setSelectedSymbol(symbol)}>{symbol}</button>
            ))}
            {!(daemonRunning && snapshot.data?.strategy_symbols?.length) ? <em>—</em> : null}
          </div>
        </div>
        <div>
          <span>{t("liveObserverSubscriptions")}</span>
          <div className="live-subscription-list">
            {(daemonRunning ? snapshot.data?.observer_symbols || [] : []).map((symbol) => (
              <button type="button" key={symbol} onClick={() => setSelectedSymbol(symbol)}>
                {symbol}{awaitingFirstTick.has(symbol) ? ` · ${t("liveAwaitingFirstTick")}` : ""}
              </button>
            ))}
            {!(daemonRunning && snapshot.data?.observer_symbols?.length) ? <em>—</em> : null}
          </div>
        </div>
      </div>
      <DataTable rows={marketRows} columns={columns} loading={snapshot.loading} empty={t("liveMarketEmpty")} />
      <div className="live-market-chart-head">
        <div>
          <h3>{selectedSymbol || t("kline")}</h3>
          <span className="muted">{bars.data?.date_range?.length ? `${bars.data.date_range[0]} - ${bars.data.date_range[1]}` : "-"}</span>
        </div>
        <Tabs
          tabs={[{ key: "60", label: t("liveBar1m") }, { key: "300", label: t("liveBar5m") }]}
          active={String(interval)}
          onChange={(value) => setIntervalSeconds(Number(value))}
        />
      </div>
      {bars.error ? <Alert tone="error">{bars.error}</Alert> : null}
      {bars.loading && !chartRows.length ? <Spinner /> : null}
      {chartRows.length ? (
        <CandlestickPlot
          rows={chartRows}
          label={selectedSymbol}
          metricLabel={t("klineMetricVolume")}
          metricValues={chartRows.map((row) => row.volume)}
          barColors={chartColors}
          hoverText={chartHover}
          xaxisType="category"
          tickvals={tickIndexes}
          ticktext={tickIndexes.map((index) => timeLabel(chartRows[index].date))}
          compact
        />
      ) : !bars.loading ? <div className="empty">{t("liveMarketBarsEmpty")}</div> : null}
    </section>
  );
}
