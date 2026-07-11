import { useMemo, useState } from "react";
import { AsyncButton, DataTable, StatusPill } from "../../components";
import { useI18n } from "../../i18n";
import type { AsyncResource, LiveDaemonStatus, LiveLedgerEvent, LiveLedgerEvents, LiveOrder, LivePosition, LiveTrade } from "./types";
import { compactJson, fmtMoney } from "./utils";
import { LiveMarketPanel } from "./LiveMarketPanel";

type Tab = "positions" | "orders" | "trades" | "market" | "audit";

type Props = {
  daemon?: LiveDaemonStatus | null;
  mode: string;
  tradeBroker: string;
  quoteProvider: string;
  ledger: AsyncResource<LiveLedgerEvents>;
  ledgerKind: string;
  setLedgerKind: (value: string) => void;
  ledgerReference: string;
  setLedgerReference: (value: string) => void;
  ledgerLimit: string;
  setLedgerLimit: (value: string) => void;
  onCancelOrder: (order: LiveOrder) => void | Promise<unknown>;
};

function value(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? fmtMoney(number) : "—";
}

export function LiveActivityTabs(props: Props) {
  const { t } = useI18n();
  const [tab, setTab] = useState<Tab>("positions");
  const [activeOnly, setActiveOnly] = useState(true);
  const positions = props.daemon?.state?.positions || [];
  const allOrders = props.daemon?.state?.orders || [];
  const orders = useMemo(() => activeOnly ? allOrders.filter((order) => order.active) : allOrders, [activeOnly, allOrders]);
  const trades = props.daemon?.state?.trades || [];
  const events = props.ledger.data?.events || [];
  const tabs: Array<{ key: Tab; label: string; count: number | null }> = [
    { key: "positions", label: t("livePositions"), count: positions.length },
    { key: "orders", label: t("liveOrders"), count: allOrders.filter((order) => order.active).length },
    { key: "trades", label: t("liveTrades"), count: trades.length },
    { key: "market", label: t("liveMarket"), count: props.daemon?.state?.engine?.ticks ?? null },
    { key: "audit", label: t("liveLedger"), count: props.ledger.data?.count ?? events.length },
  ];

  return (
    <section className="panel live-activity" aria-labelledby="live-activity-title">
      <h2 id="live-activity-title" className="sr-only">{t("liveBusinessData")}</h2>
      <div className="live-business-tabs" role="tablist" aria-label={t("liveBusinessData")}>
        {tabs.map((item) => (
          <button type="button" role="tab" aria-selected={tab === item.key} className={tab === item.key ? "active" : ""} onClick={() => setTab(item.key)} key={item.key}>
            {item.label}{item.count !== null ? <span className="live-count-badge">{item.count}</span> : null}
          </button>
        ))}
      </div>
      <div role="tabpanel" className="live-tab-content">
        {tab === "positions" ? (
          <DataTable<LivePosition> rows={positions} empty={t("empty")} columns={[
            { key: "code", label: t("liveCode") },
            { key: "exchange", label: t("liveExchange") },
            { key: "volume", label: t("liveVolume"), align: "right" },
            { key: "available", label: t("liveAvailable"), align: "right" },
            { key: "frozen", label: t("livePositionFrozen"), align: "right" },
            { key: "price", label: t("liveAvgPrice"), align: "right", render: (row) => value(row.price) },
            { key: "pnl", label: t("livePositionPnl"), align: "right", render: (row) => value(row.pnl) },
            { key: "pnl_pct", label: t("livePositionPnlPct"), align: "right", render: (row) => {
              const base = Number(row.price) * Number(row.volume);
              const pnl = Number(row.pnl);
              return Number.isFinite(base) && base > 0 && Number.isFinite(pnl) ? `${(pnl / base * 100).toFixed(2)}%` : "—";
            } },
          ]} />
        ) : null}
        {tab === "orders" ? (
          <div className="stack compact">
            <div className="live-filter-row">
              <button type="button" className={activeOnly ? "button small" : "button ghost small"} onClick={() => setActiveOnly(true)}>{t("liveActiveOrders")}</button>
              <button type="button" className={!activeOnly ? "button small" : "button ghost small"} onClick={() => setActiveOnly(false)}>{t("liveAllOrders")}</button>
            </div>
            <DataTable<LiveOrder> rows={orders} empty={t("empty")} columns={[
              { key: "order_id", label: t("liveOrderId"), ellipsis: true },
              { key: "code", label: t("liveCode") },
              { key: "side", label: t("liveSideCol"), render: (row) => t(row.side === "buy" ? "liveBuy" : "liveSell") },
              { key: "price", label: t("livePrice"), align: "right", render: (row) => value(row.price) },
              { key: "progress", label: t("liveOrderProgress"), align: "right", render: (row) => `${row.traded}/${row.volume}` },
              { key: "status", label: t("status"), render: (row) => <StatusPill status={row.status} /> },
              { key: "action", label: t("action"), render: (row) => row.active ? <AsyncButton className="button small danger" onClick={() => props.onCancelOrder(row)}>{t("liveCancelOrder")}</AsyncButton> : "—" },
            ]} />
          </div>
        ) : null}
        {tab === "trades" ? <DataTable<LiveTrade> rows={[...trades].reverse()} empty={t("empty")} columns={[
          { key: "trade_id", label: t("tradeId"), ellipsis: true },
          { key: "code", label: t("liveCode") },
          { key: "side", label: t("liveSideCol"), render: (row) => t(row.side === "buy" ? "liveBuy" : "liveSell") },
          { key: "price", label: t("livePrice"), align: "right", render: (row) => value(row.price) },
          { key: "volume", label: t("liveVolume"), align: "right" },
        ]} /> : null}
        {tab === "market" ? <LiveMarketPanel mode={props.mode} tradeBroker={props.tradeBroker} quoteProvider={props.quoteProvider} daemonRunning={Boolean(props.daemon?.running)} embedded /> : null}
        {tab === "audit" ? (
          <div className="stack compact">
            <div className="live-filter-row">
              <label className="field"><span>{t("kind")}</span><input value={props.ledgerKind} onChange={(event) => props.setLedgerKind(event.target.value)} placeholder="submit" /></label>
              <label className="field"><span>{t("liveReference")}</span><input value={props.ledgerReference} onChange={(event) => props.setLedgerReference(event.target.value)} /></label>
              <label className="field"><span>{t("limit")}</span><input value={props.ledgerLimit} onChange={(event) => props.setLedgerLimit(event.target.value)} inputMode="numeric" /></label>
            </div>
            <DataTable<LiveLedgerEvent> rows={events} loading={props.ledger.loading} empty={t("empty")} columns={[
              { key: "ts", label: t("time"), ellipsis: true },
              { key: "kind", label: t("kind") },
              { key: "reference", label: t("liveReference"), ellipsis: true },
              { key: "order_id", label: t("liveOrderId"), ellipsis: true },
              { key: "payload", label: t("payload"), ellipsis: true, render: (row) => compactJson(row.payload) },
            ]} />
          </div>
        ) : null}
      </div>
    </section>
  );
}
