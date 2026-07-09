import { useMemo } from "react";
import { chartHeight, DataTable } from "../../components";
import { LazyPlot as Plot } from "../../components/LazyPlot";
import { useI18n } from "../../i18n";
import type { TablePreview, TimingDetailPayload } from "./types";

export function previewColumns(table: TablePreview | undefined, fallback: string[] = []): Array<{ key: string; label: string; ellipsis?: boolean; align?: "left" | "right" | "center" }> {
  const keys = (table?.columns?.length ? table.columns : fallback).slice(0, 10);
  return keys.map((key) => ({
    key,
    label: key,
    ellipsis: ["datetime", "signal_datetime", "instrument", "reason"].includes(key) ? undefined : true,
    align: ["signal", "target_percent", "score", "amount", "price", "fee", "equity", "cash"].includes(key) ? "right" : undefined,
  }));
}

function toFiniteNumber(value: unknown): number | undefined {
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
}

function formatRatioPercent(value: unknown): string {
  const n = toFiniteNumber(value);
  return n === undefined ? "-" : `${n >= 0 ? "+" : ""}${(n * 100).toFixed(2)}%`;
}

function formatMoney(value: unknown): string {
  const n = toFiniteNumber(value);
  if (n === undefined) return "-";
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(n);
}

function timingEquitySeries(rows: Array<Record<string, unknown>>): Array<{ datetime: string; equity: number }> {
  const byTime = new Map<string, number>();
  rows.forEach((row) => {
    const dt = row.datetime ? String(row.datetime) : "";
    const equity = toFiniteNumber(row.equity);
    if (dt && equity !== undefined && !byTime.has(dt)) byTime.set(dt, equity);
  });
  return [...byTime.entries()]
    .map(([datetime, equity]) => ({ datetime, equity }))
    .sort((a, b) => a.datetime.localeCompare(b.datetime));
}

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

export function TimingResultPanel({ detail }: { detail: TimingDetailPayload }) {
  const { t } = useI18n();
  const summary = detail.summary || {};
  const equity = useMemo(() => timingEquitySeries(detail.equity_curve.rows || []), [detail]);
  const metricRows = [
    [t("timingFinalEquity"), formatMoney(summary.final_equity)],
    [t("timingTotalReturn"), formatRatioPercent(summary.total_return)],
    [t("timingAnnualReturn"), formatRatioPercent(summary.annual_return)],
    [t("timingMaxDrawdown"), formatRatioPercent(summary.max_drawdown)],
    [t("timingTrades"), String(summary.n_trades ?? "-")],
    [t("timingTotalFee"), formatMoney(summary.total_fee)],
  ];

  return (
    <>
      <section className="panel">
        <div className="panel-head">
          <h2>{t("timingBacktestResult")}</h2>
          <span className="muted">{detail.artifact_dir}</span>
        </div>
        <div className="metric-grid">
          {metricRows.map(([label, value]) => (
            <div className="metric" key={label}>
              <span className="metric-label">{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </div>
        {equity.length ? (
          <Plot
            data={[
              {
                x: equity.map((row) => row.datetime),
                y: equity.map((row) => row.equity),
                type: "scatter",
                mode: "lines",
                name: t("equityCurve"),
                line: { color: cssVar("--accent-600", "#2563eb"), width: 2 },
              },
            ]}
            layout={{
              autosize: true,
              height: chartHeight(),
              margin: { l: 50, r: 20, t: 20, b: 45 },
              hovermode: "x unified",
              paper_bgcolor: "rgba(0,0,0,0)",
              plot_bgcolor: "rgba(0,0,0,0)",
            }}
            config={{ responsive: true, displayModeBar: false }}
            style={{ width: "100%" }}
          />
        ) : (
          <div className="empty">{t("empty")}</div>
        )}
      </section>
      <section className="panel">
        <h2>{t("timingTrades")}</h2>
        <DataTable
          rows={detail.trades.rows}
          empty={t("empty")}
          columns={previewColumns(detail.trades, ["datetime", "instrument", "side", "amount", "price", "fee", "reason"])}
        />
      </section>
      <div className="grid two">
        <section className="panel">
          <h2>{t("timingPositions")}</h2>
          <DataTable
            rows={detail.positions.rows}
            empty={t("empty")}
            columns={previewColumns(detail.positions, ["datetime", "instrument", "amount", "market_value"])}
          />
        </section>
        <section className="panel">
          <h2>{t("timingSignals")}</h2>
          <DataTable
            rows={detail.signals.rows}
            empty={t("empty")}
            columns={previewColumns(detail.signals, ["datetime", "instrument", "signal", "target_percent", "score", "reason"])}
          />
        </section>
      </div>
    </>
  );
}
