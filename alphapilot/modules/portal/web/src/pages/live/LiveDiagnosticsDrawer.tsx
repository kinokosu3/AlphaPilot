import { useEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";
import { Alert, AsyncButton, DataTable, StatusPill } from "../../components";
import { useI18n } from "../../i18n";
import type {
  AsyncResource,
  LiveBrokerSpec,
  LiveCommandStatus,
  LiveConfigSnapshot,
  LiveDaemonStatus,
  LiveLedgerEvent,
  LivePluginDiagnostics,
  LivePreflight,
  LiveQuoteProviderSpec,
  LiveRiskStatus,
  LiveRuntimeState,
} from "./types";
import { fmtMoney } from "./utils";

type Props = {
  open: boolean;
  onClose: () => void;
  workspace: "live" | "shadow" | "simulation" | "paper";
  runtimeBroker: string;
  setRuntimeBroker: Dispatch<SetStateAction<string>>;
  runtimeQuoteProvider: string;
  setRuntimeQuoteProvider: Dispatch<SetStateAction<string>>;
  brokers: LiveBrokerSpec[];
  quoteProviders: LiveQuoteProviderSpec[];
  providerSelectionLocked: boolean;
  providerReady: boolean;
  cfg?: LiveConfigSnapshot;
  daemon?: LiveDaemonStatus | null;
  runtimeState: AsyncResource<LiveRuntimeState>;
  riskStatus: AsyncResource<LiveRiskStatus>;
  pluginDiagnostics: AsyncResource<LivePluginDiagnostics>;
  preflight: LivePreflight | null;
  preflightNetwork: boolean;
  setPreflightNetwork: Dispatch<SetStateAction<boolean>>;
  onPreflight: () => void | Promise<unknown>;
  onConnect: () => void | Promise<unknown>;
};

export function LiveDiagnosticsDrawer(props: Props) {
  const { t } = useI18n();
  const closeRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!props.open) return;
    const previous = document.activeElement as HTMLElement | null;
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") props.onClose();
    };
    document.addEventListener("keydown", handleKey);
    window.setTimeout(() => closeRef.current?.focus(), 0);
    return () => {
      document.removeEventListener("keydown", handleKey);
      previous?.focus?.();
    };
  }, [props.open, props.onClose]);

  if (!props.open) return null;
  const selectedBroker = props.brokers.find((item) => item.name === props.runtimeBroker);
  const selectedQuote = props.quoteProviders.find((item) => item.name === props.runtimeQuoteProvider);
  const risk = props.riskStatus.data?.risk;
  const recovery = props.riskStatus.data?.recovery;
  const rejections = props.riskStatus.data?.recent_rejections || [];
  const commands = props.daemon?.command_status_tail || [];

  return (
    <div className="live-drawer-layer">
      <button type="button" className="live-drawer-backdrop" aria-label={t("cancel")} onClick={props.onClose} />
      <aside className="live-diagnostics-drawer" role="dialog" aria-modal="true" aria-labelledby="live-diagnostics-title">
        <header>
          <div><h2 id="live-diagnostics-title">{t("liveDiagnostics")}</h2><p>{t("liveDiagnosticsHint")}</p></div>
          <button ref={closeRef} type="button" className="button ghost icon-only" aria-label={t("cancel")} onClick={props.onClose}>×</button>
        </header>
        <div className="live-drawer-content">
          <section>
            <h3>{t("liveConnectionSettings")}</h3>
            {props.workspace !== "paper" ? (
              <div className="live-form-grid">
                <label className="field"><span>{t("liveTradeBroker")}</span><select aria-label={t("liveTradeBroker")} value={props.runtimeBroker} onChange={(event) => props.setRuntimeBroker(event.target.value)} disabled={props.providerSelectionLocked}>{props.brokers.map((item) => <option value={item.name} disabled={!item.gateway_importable} key={item.name}>{item.name} - {item.description}</option>)}</select></label>
                <label className="field"><span>{t("liveQuoteProvider")}</span><select aria-label={t("liveQuoteProvider")} value={props.runtimeQuoteProvider} onChange={(event) => props.setRuntimeQuoteProvider(event.target.value)} disabled={props.providerSelectionLocked}>{props.quoteProviders.map((item) => <option value={item.name} disabled={!item.gateway_importable} key={item.name}>{item.name} - {item.description}</option>)}</select></label>
              </div>
            ) : <Alert tone="info">{t("livePaperDaemonHint")}</Alert>}
            {props.providerSelectionLocked ? <Alert tone="info">{t("liveProviderLocked")}</Alert> : null}
            <label className="inline-check compact"><input type="checkbox" checked={props.preflightNetwork} onChange={(event) => props.setPreflightNetwork(event.target.checked)} /><span>{t("liveNetworkCheck")}</span></label>
            <div className="row-actions">
              <AsyncButton className="button small" onClick={props.onPreflight} disabled={!props.providerReady}>{t("livePreflight")}</AsyncButton>
              <AsyncButton className="button ghost small" onClick={props.onConnect} disabled={props.workspace === "paper" || !props.providerReady}>{t("liveRuntimeConnect")}</AsyncButton>
            </div>
            {selectedBroker ? <div className="live-diagnostic-summary"><span>{t("liveBrokerStatus")}</span><StatusPill status={selectedBroker.gateway_importable ? "ready" : "missing"} /><strong>{selectedBroker.distribution || selectedBroker.plugin_id || selectedBroker.gateway}</strong></div> : null}
            {selectedQuote && selectedQuote.name !== selectedBroker?.name ? <div className="live-diagnostic-summary"><span>{t("liveQuoteStatus")}</span><StatusPill status={selectedQuote.gateway_importable ? "ready" : "missing"} /><strong>{selectedQuote.distribution || selectedQuote.plugin_id || selectedQuote.gateway}</strong></div> : null}
            {selectedBroker ? (
              <div className="metric-grid compact">
                <div className="metric"><span className="metric-label">{t("liveMissingEnv")}</span><strong>{selectedBroker.missing_env.length ? selectedBroker.missing_env.join(", ") : "—"}</strong></div>
                <div className="metric"><span className="metric-label">{t("liveCapabilities")}</span><strong>{Object.values(selectedBroker.capabilities).filter((item) => item === true).length}</strong></div>
              </div>
            ) : null}
            {props.preflight ? <div className="live-diagnostic-summary"><span>{t("livePreflight")}</span><StatusPill status={props.preflight.ok ? "ok" : "blocked"} /><strong>{props.preflight.missing_env.length ? props.preflight.missing_env.join(", ") : t("liveNoMissingEnv")}</strong></div> : null}
            {props.preflight?.endpoints.length ? (
              <DataTable rows={props.preflight.endpoints} empty={t("empty")} columns={[
                { key: "name", label: t("name") },
                { key: "host", label: t("host"), ellipsis: true },
                { key: "port", label: t("port"), align: "right" },
                { key: "ok", label: t("status"), render: (row) => <StatusPill status={row.ok ? "ok" : "blocked"} /> },
                { key: "detail", label: t("result"), ellipsis: true },
              ]} />
            ) : null}
            {props.runtimeState.error ? <Alert tone="error">{props.runtimeState.error}</Alert> : null}
          </section>

          <section>
            <h3>{t("liveRiskRecovery")}</h3>
            {props.riskStatus.error ? <Alert tone="error">{props.riskStatus.error}</Alert> : null}
            <div className="metric-grid compact">
              <div className="metric"><span className="metric-label">{t("liveRiskOrdersToday")}</span><strong>{risk?.orders_today ?? "—"}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRiskValueToday")}</span><strong>{risk ? fmtMoney(risk.value_today ?? 0) : "—"}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRecentRejections")}</span><strong>{rejections.length}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRecovery")}</span><StatusPill status={recovery?.risk_restored ? "restored" : "pending"} /></div>
              <div className="metric"><span className="metric-label">{t("liveRiskMaxOrder")}</span><strong>{props.cfg ? fmtMoney(props.cfg.risk.max_order_value) : "—"}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRiskMaxDaily")}</span><strong>{props.cfg ? fmtMoney(props.cfg.risk.max_daily_value) : "—"}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRiskMaxPos")}</span><strong>{props.cfg ? `${Math.round((props.cfg.risk.max_position_pct || 0) * 100)}%` : "—"}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRiskPriceGuard")}</span><strong>{props.cfg ? `${Math.round((props.cfg.risk.price_guard_pct || 0) * 100)}%` : "—"}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRiskLot")}</span><strong>{props.cfg?.risk.lot_size ?? "—"}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRiskMaxOrders")}</span><strong>{props.cfg?.risk.max_orders_per_day ?? "—"}</strong></div>
            </div>
            {recovery?.warnings?.length ? <Alert tone="info">{recovery.warnings.map((item) => item.detail || item.kind).filter(Boolean).join(", ")}</Alert> : null}
            <DataTable<LiveLedgerEvent> rows={rejections} empty={t("empty")} columns={[
              { key: "ts", label: t("time"), ellipsis: true },
              { key: "reference", label: t("liveReference"), ellipsis: true },
              { key: "payload", label: t("reason"), ellipsis: true, render: (row) => String(row.payload?.reason || row.payload?.rule || "") },
            ]} />
          </section>

          <section>
            <h3>{t("livePluginDiagnostics")}</h3>
            {props.pluginDiagnostics.error ? <Alert tone="error">{props.pluginDiagnostics.error}</Alert> : null}
            {props.pluginDiagnostics.data?.issues.map((issue) => <Alert tone="error" key={`${issue.plugin_id}-${issue.kind}`}>{issue.plugin_id}: {issue.error}</Alert>)}
            <DataTable rows={(props.pluginDiagnostics.data?.plugins || []) as Array<Record<string, unknown>>} empty={t("empty")} columns={[
              { key: "plugin_id", label: "Plugin" },
              { key: "distribution", label: t("livePluginPackage"), ellipsis: true },
              { key: "status", label: t("status"), render: (row) => <StatusPill status={String(row.status || "")} /> },
            ]} />
          </section>

          <section>
            <h3>{t("liveDaemonTechnical")}</h3>
            <div className="live-technical-list">
              <span><small>PID</small><strong>{props.daemon?.pid ?? "—"}</strong></span>
              <span><small>{t("liveStateFile")}</small><strong>{props.daemon?.state_path || props.runtimeState.data?.state_path || "—"}</strong></span>
              <span><small>{t("liveLogFile")}</small><strong>{props.daemon?.log_path || "—"}</strong></span>
              <span><small>{t("liveCommands")}</small><strong>{props.daemon?.commands_processed ?? 0}</strong></span>
            </div>
            <DataTable<LiveCommandStatus> rows={[...commands].reverse().slice(0, 20)} empty={t("empty")} columns={[
              { key: "ts", label: t("time"), ellipsis: true },
              { key: "action", label: t("action") },
              { key: "stage", label: t("status"), render: (row) => <StatusPill status={String(row.stage || "")} /> },
              { key: "result", label: t("result"), ellipsis: true, render: (row) => String(row.result?.error || row.result?.message || "") },
            ]} />
          </section>
        </div>
      </aside>
    </div>
  );
}
