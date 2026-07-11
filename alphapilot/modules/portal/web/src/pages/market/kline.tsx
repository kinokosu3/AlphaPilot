import { useMemo } from "react";
import { CandlestickPlot } from "../../components/CandlestickPlot";
import { useI18n } from "../../i18n";

export type KlineMetric = "amount" | "volume" | "turn" | "pctChg";
export type KlineRange = "1M" | "3M" | "6M" | "1Y" | "ALL";

export type KlineRow = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  amount?: number;
  turn?: number;
  pctChg?: number;
};

export type KlinePayload = {
  symbol?: string;
  label?: string;
  date_range?: unknown[];
  rows?: Array<Record<string, unknown>>;
};

const KLINE_RANGES: KlineRange[] = ["1M", "3M", "6M", "1Y", "ALL"];
const KLINE_METRICS: KlineMetric[] = ["amount", "volume", "turn", "pctChg"];

function toFiniteNumber(value: unknown): number | undefined {
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
}

function normalizeKlineRows(rows: Array<Record<string, unknown>>): KlineRow[] {
  return rows.flatMap((row) => {
    const open = toFiniteNumber(row.open);
    const high = toFiniteNumber(row.high);
    const low = toFiniteNumber(row.low);
    const close = toFiniteNumber(row.close);
    const date = row.date ? String(row.date) : "";
    if (!date || open === undefined || high === undefined || low === undefined || close === undefined) return [];
    return [{
      date,
      open,
      high,
      low,
      close,
      volume: toFiniteNumber(row.volume),
      amount: toFiniteNumber(row.amount),
      turn: toFiniteNumber(row.turn),
      pctChg: toFiniteNumber(row.pctChg)
    }];
  });
}

function metricValue(row: KlineRow | undefined, metric: KlineMetric, prev?: KlineRow): number | undefined {
  if (!row) return undefined;
  if (metric === "pctChg") {
    if (row.pctChg !== undefined) return row.pctChg;
    if (prev?.close) return ((row.close - prev.close) / prev.close) * 100;
    return undefined;
  }
  return row[metric];
}

function hasMetric(rows: KlineRow[], metric: KlineMetric): boolean {
  return rows.some((row, index) => metricValue(row, metric, rows[index - 1]) !== undefined);
}

function resolveKlineMetric(rows: KlineRow[], metric: KlineMetric): KlineMetric {
  if (hasMetric(rows, metric)) return metric;
  if (hasMetric(rows, "volume")) return "volume";
  return metric;
}

function klineRangeValue(rows: KlineRow[], range: KlineRange): [string, string] | undefined {
  if (range === "ALL" || rows.length < 2) return undefined;
  const lastDate = new Date(rows[rows.length - 1].date);
  if (Number.isNaN(lastDate.getTime())) return undefined;
  const startDate = new Date(lastDate);
  if (range === "1M") startDate.setMonth(startDate.getMonth() - 1);
  if (range === "3M") startDate.setMonth(startDate.getMonth() - 3);
  if (range === "6M") startDate.setMonth(startDate.getMonth() - 6);
  if (range === "1Y") startDate.setFullYear(startDate.getFullYear() - 1);
  return [startDate.toISOString().slice(0, 10), rows[rows.length - 1].date];
}

// Intraday bars carry a non-midnight time component. Use a category axis so lunch breaks
// and overnight gaps do not render as empty spans.
export function klineIsIntraday(rows: KlineRow[]): boolean {
  return rows.some((row) => {
    const m = String(row.date).match(/[ T](\d{2}):(\d{2})/);
    return m ? m[1] !== "00" || m[2] !== "00" : false;
  });
}

export function klineAxisType(rows: KlineRow[]): "date" | "category" {
  return klineIsIntraday(rows) ? "category" : "date";
}

function klineCategoryRange(rows: KlineRow[], range: KlineRange): [number, number] | undefined {
  const win = klineRangeValue(rows, range);
  if (!win) return undefined;
  const startTs = new Date(win[0]).getTime();
  const startIdx = rows.findIndex((row) => new Date(row.date).getTime() >= startTs);
  if (startIdx <= 0) return undefined;
  return [startIdx - 0.5, rows.length - 0.5];
}

export function klineTimeLabel(date: unknown): string {
  const m = String(date).match(/[ T](\d{2}:\d{2})/);
  return m ? m[1] : String(date).slice(0, 10);
}

export function klineCategoryTicks(
  rows: KlineRow[],
  range: [number, number] | undefined,
  maxTicks = 7,
): { tickvals: number[]; ticktext: string[] } {
  const lo = range ? Math.max(0, Math.ceil(range[0])) : 0;
  const hi = range ? Math.min(rows.length - 1, Math.floor(range[1])) : rows.length - 1;
  const span = hi - lo;
  if (span < 0) return { tickvals: [], ticktext: [] };
  const count = Math.min(maxTicks, span + 1);
  const idxs = count <= 1
    ? [lo]
    : Array.from({ length: count }, (_, i) => Math.round(lo + (span * i) / (count - 1)));
  const tickvals = Array.from(new Set(idxs));
  return { tickvals, ticktext: tickvals.map((i) => klineTimeLabel(rows[i].date)) };
}

function formatDateLabel(value: unknown): string {
  return value ? String(value).slice(0, 10) : "-";
}

function formatPrice(value?: number): string {
  return value === undefined ? "-" : value.toFixed(2);
}

function formatCompactNumber(value?: number): string {
  if (value === undefined) return "-";
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 2 }).format(value);
}

function formatPercent(value?: number): string {
  return value === undefined ? "-" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

type MarketKlineViewerProps = {
  payload: KlinePayload | null;
  symbol: string;
  range: KlineRange;
  onRangeChange: (range: KlineRange) => void;
  subMetric: KlineMetric;
  onSubMetricChange: (metric: KlineMetric) => void;
};

export function MarketKlineViewer({
  payload,
  symbol,
  range,
  onRangeChange,
  subMetric,
  onSubMetricChange
}: MarketKlineViewerProps) {
  const { t } = useI18n();
  const rows = useMemo(() => normalizeKlineRows(payload?.rows || []), [payload]);
  const chartMetric = resolveKlineMetric(rows, subMetric);
  const latestRow = rows[rows.length - 1];
  const previousRow = rows[rows.length - 2];
  const latestPct = latestRow ? metricValue(latestRow, "pctChg", previousRow) : undefined;
  const subMetricValues = rows.map((row, index) => metricValue(row, chartMetric, rows[index - 1]) ?? null);
  const volumeColors = rows.map((row) => (row.close >= row.open ? "rgba(239, 83, 80, 0.72)" : "rgba(38, 166, 154, 0.72)"));
  const klineLabel = String(payload?.label || payload?.symbol || symbol || t("kline"));
  const klineDateRange = Array.isArray(payload?.date_range)
    ? `${formatDateLabel(payload?.date_range[0])} - ${formatDateLabel(payload?.date_range[1])}`
    : rows.length
      ? `${formatDateLabel(rows[0].date)} - ${formatDateLabel(rows[rows.length - 1].date)}`
      : "-";
  const metricLabels: Record<KlineMetric, string> = {
    amount: t("klineMetricAmount"),
    volume: t("klineMetricVolume"),
    turn: t("klineMetricTurn"),
    pctChg: t("klineMetricPct")
  };
  const klineXAxisType = klineAxisType(rows);
  const chartRange = klineXAxisType === "category"
    ? klineCategoryRange(rows, range)
    : klineRangeValue(rows, range);
  const intradayTicks = klineXAxisType === "category"
    ? klineCategoryTicks(rows, chartRange as [number, number] | undefined)
    : undefined;
  const klineHoverText = rows.map((row, index) => {
    const pct = metricValue(row, "pctChg", rows[index - 1]);
    return [
      `<b>${formatDateLabel(row.date)}</b>`,
      `${t("klineOpen")}: ${formatPrice(row.open)}`,
      `${t("klineHigh")}: ${formatPrice(row.high)}`,
      `${t("klineLow")}: ${formatPrice(row.low)}`,
      `${t("klineClose")}: ${formatPrice(row.close)}`,
      `${t("klineMetricPct")}: ${formatPercent(pct)}`,
      `${t("klineMetricAmount")}: ${formatCompactNumber(row.amount)}`,
      `${t("klineMetricVolume")}: ${formatCompactNumber(row.volume)}`,
      `${t("klineMetricTurn")}: ${formatPercent(row.turn)}`
    ].join("<br>");
  });

  if (!rows.length) return <div className="empty">{t("empty")}</div>;

  return (
    <section className="panel kline-panel">
      <div className="kline-chart-head">
        <div>
          <h2>{klineLabel}</h2>
          <p>{klineDateRange}</p>
        </div>
        <div className="kline-controls">
          <div className="kline-range-buttons" aria-label={t("klineRange")}>
            {KLINE_RANGES.map((item) => (
              <button
                key={item}
                className={item === range ? "active" : ""}
                onClick={() => onRangeChange(item)}
                type="button"
              >
                {item === "ALL" ? t("klineRangeAll") : item}
              </button>
            ))}
          </div>
          <select value={subMetric} onChange={(e) => onSubMetricChange(e.target.value as KlineMetric)} aria-label={t("klineSubMetric")}>
            {KLINE_METRICS.map((metric) => <option key={metric} value={metric}>{metricLabels[metric]}</option>)}
          </select>
        </div>
      </div>
      <div className="kline-stats">
        <div>
          <span>{t("klineClose")}</span>
          <strong>{formatPrice(latestRow?.close)}</strong>
        </div>
        <div className={latestPct !== undefined && latestPct >= 0 ? "up" : "down"}>
          <span>{t("klineMetricPct")}</span>
          <strong>{formatPercent(latestPct)}</strong>
        </div>
        <div>
          <span>{t("klineHigh")}</span>
          <strong>{formatPrice(latestRow?.high)}</strong>
        </div>
        <div>
          <span>{t("klineLow")}</span>
          <strong>{formatPrice(latestRow?.low)}</strong>
        </div>
        <div>
          <span>{metricLabels[chartMetric]}</span>
          <strong>
            {chartMetric === "pctChg" || chartMetric === "turn"
              ? formatPercent(metricValue(latestRow, chartMetric, previousRow))
              : formatCompactNumber(metricValue(latestRow, chartMetric, previousRow))}
          </strong>
        </div>
      </div>
      {chartMetric !== subMetric ? <p className="kline-note">{t("klineMetricFallback")}</p> : null}
      <CandlestickPlot
        rows={rows}
        label={klineLabel}
        metricLabel={metricLabels[chartMetric]}
        metricValues={subMetricValues}
        barColors={volumeColors}
        hoverText={klineHoverText}
        xaxisType={klineXAxisType}
        range={chartRange}
        tickvals={intradayTicks?.tickvals}
        ticktext={intradayTicks?.ticktext}
      />
    </section>
  );
}
