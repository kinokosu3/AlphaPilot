import { chartHeight } from "../components";
import { LazyPlot as Plot } from "./LazyPlot";

export type CandlestickPlotRow = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
};

type Props = {
  rows: CandlestickPlotRow[];
  label: string;
  metricLabel: string;
  metricValues: Array<number | null>;
  barColors: string[];
  hoverText: string[];
  xaxisType: "date" | "category";
  range?: [string, string] | [number, number];
  tickvals?: number[];
  ticktext?: string[];
  compact?: boolean;
};

function cssVar(name: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

export function CandlestickPlot({
  rows,
  label,
  metricLabel,
  metricValues,
  barColors,
  hoverText,
  xaxisType,
  range,
  tickvals,
  ticktext,
  compact = false,
}: Props) {
  const colors = {
    surface: cssVar("--surface", "#ffffff"),
    surface2: cssVar("--surface-2", "#f7f8fc"),
    border: cssVar("--border", "#e3e6ef"),
    text: cssVar("--text", "#1a2233"),
    muted: cssVar("--text-muted", "#667085"),
    up: "#ef5350",
    down: "#26a69a",
  };
  const categoryTicks = xaxisType === "category" && tickvals && ticktext
    ? { tickmode: "array" as const, tickvals, ticktext }
    : {};

  return (
    <Plot
      data={[
        {
          x: rows.map((row) => row.date),
          open: rows.map((row) => row.open),
          high: rows.map((row) => row.high),
          low: rows.map((row) => row.low),
          close: rows.map((row) => row.close),
          type: "candlestick",
          name: label,
          text: hoverText,
          hovertemplate: "%{text}<extra></extra>",
          increasing: { line: { color: colors.up, width: 1.1 }, fillcolor: colors.up },
          decreasing: { line: { color: colors.down, width: 1.1 }, fillcolor: colors.down },
          xaxis: "x",
          yaxis: "y",
        },
        {
          x: rows.map((row) => row.date),
          y: metricValues,
          type: "bar",
          name: metricLabel,
          marker: { color: barColors },
          hovertemplate: `<b>%{x}</b><br>${metricLabel}: %{y:.2f}<extra></extra>`,
          xaxis: "x2",
          yaxis: "y2",
        },
      ]}
      layout={{
        autosize: true,
        height: compact ? Math.max(420, Math.min(600, chartHeight() + 60)) : Math.max(560, Math.min(780, chartHeight() + 180)),
        margin: { l: 18, r: 64, t: 10, b: 34 },
        paper_bgcolor: colors.surface,
        plot_bgcolor: colors.surface,
        font: { color: colors.text, size: 12 },
        dragmode: "pan",
        hovermode: "x unified",
        showlegend: false,
        bargap: 0,
        xaxis: {
          domain: [0, 1], anchor: "y", type: xaxisType, range,
          rangeslider: { visible: false }, showgrid: true, gridcolor: colors.border,
          showline: true, linecolor: colors.border, tickfont: { color: colors.muted },
          showspikes: true, spikemode: "across", spikesnap: "cursor",
          spikecolor: colors.muted, spikethickness: 1, ...categoryTicks,
        },
        xaxis2: {
          domain: [0, 1], anchor: "y2", matches: "x", type: xaxisType,
          showgrid: true, gridcolor: colors.border, showline: true, linecolor: colors.border,
          tickfont: { color: colors.muted }, showspikes: true, spikemode: "across",
          spikesnap: "cursor", spikecolor: colors.muted, spikethickness: 1, ...categoryTicks,
        },
        yaxis: {
          domain: [0.28, 1], side: "right", fixedrange: false, showgrid: true,
          gridcolor: colors.border, zeroline: false, tickfont: { color: colors.muted },
        },
        yaxis2: {
          domain: [0, 0.22], side: "right", fixedrange: false, showgrid: true,
          gridcolor: colors.border, zeroline: false,
          title: { text: metricLabel, font: { color: colors.muted, size: 11 } },
          tickfont: { color: colors.muted },
        },
        hoverlabel: { bgcolor: colors.surface2, bordercolor: colors.border, font: { color: colors.text } },
      }}
      config={{
        responsive: true,
        displaylogo: false,
        modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d"],
      }}
      useResizeHandler
      style={{ width: "100%" }}
    />
  );
}
