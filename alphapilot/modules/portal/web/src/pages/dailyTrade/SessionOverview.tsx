import { useMemo } from "react";
import { chartHeight } from "../../components";
import { LazyPlot as Plot } from "../../components/LazyPlot";
import { useI18n } from "../../i18n";
import { computeSessionPnl } from "../../sessionPnl";
import type { TradeSessionDetail } from "./types";

const PNL_MARGIN = { l: 56, r: 48, t: 28, b: 40 };
const fmtMoney = (v: number | null): string => (v === null ? "—" : (v > 0 ? "+" : "") + Math.round(v).toLocaleString());
const fmtPts = (v: number): string => (v > 0 ? "+" : "") + v.toFixed(2);

function fmtNum(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  return Number.isNaN(n) ? String(v) : n.toLocaleString();
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export function SessionOverview({ detail }: { detail: TradeSessionDetail }) {
  const { t } = useI18n();
  const pnl = useMemo(
    () => computeSessionPnl(detail.history || [], detail.manifest?.init_cash ?? 0, detail.cashflows || []),
    [detail],
  );
  const nPositions = detail.state?.positions ? Object.keys(detail.state.positions).length : 0;
  if (!pnl.hasData) return <p className="empty">{t("pnlNoData")}</p>;
  return (
    <>
      <div className="metric-grid compact">
        <Metric label={t("pnlMoney")} value={fmtMoney(pnl.totals.pnlMoney)} />
        <Metric label={t("cumReturnPts")} value={fmtPts(pnl.totals.cumReturnPts)} />
        <Metric label={t("totalFees")} value={fmtNum(Math.round(pnl.totals.totalFees))} />
        <Metric label={t("navLabel")} value={fmtNum(pnl.totals.latestNav)} />
        <Metric label={t("currentCash")} value={fmtNum(detail.state?.cash)} />
        <Metric label={t("positions")} value={String(nPositions)} />
      </div>
      <h4 className="muted compact">{t("equityCurve")}</h4>
      <Plot
        data={[
          { x: pnl.dates, y: pnl.nav, type: "scatter", mode: "lines", name: t("navLabel"), line: { color: "#2563eb", width: 2 } },
          { x: pnl.dates, y: pnl.cash, type: "scatter", mode: "lines", name: t("currentCash"), line: { color: "#94a3b8", width: 1.5 } },
        ]}
        layout={{ autosize: true, height: chartHeight(), margin: PNL_MARGIN, hovermode: "x unified", legend: { orientation: "h" } }}
        useResizeHandler
        style={{ width: "100%" }}
      />
      <h4 className="muted compact">{t("cumReturnChart")}</h4>
      <Plot
        data={[{ x: pnl.dates, y: pnl.cumReturnPct, type: "scatter", mode: "lines", name: t("cumReturnPts"), line: { color: "#16a34a", width: 2 } }]}
        layout={{
          autosize: true,
          height: 320,
          margin: PNL_MARGIN,
          hovermode: "x unified",
          shapes: [{ type: "line", xref: "paper", x0: 0, x1: 1, y0: 0, y1: 0, line: { dash: "dot", color: "#94a3b8" } }],
        }}
        useResizeHandler
        style={{ width: "100%" }}
      />
      <h4 className="muted compact">{t("turnoverFee")}</h4>
      <Plot
        data={[
          { x: pnl.dates, y: pnl.turnover, type: "bar", name: t("turnoverLabel"), marker: { color: "#60a5fa" }, opacity: 0.7 },
          { x: pnl.dates, y: pnl.feeMoney, type: "scatter", mode: "lines", name: t("feeMoney"), line: { color: "#ef4444", width: 1.5 }, yaxis: "y2" },
        ]}
        layout={{
          autosize: true,
          height: 320,
          margin: PNL_MARGIN,
          hovermode: "x unified",
          yaxis: { title: t("turnoverLabel") },
          yaxis2: { title: t("feeMoney"), overlaying: "y", side: "right" },
        }}
        useResizeHandler
        style={{ width: "100%" }}
      />
    </>
  );
}
