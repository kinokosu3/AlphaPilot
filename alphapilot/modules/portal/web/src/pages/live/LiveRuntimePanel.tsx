import type { Dispatch, SetStateAction } from "react";
import { Alert, AsyncButton, DataTable, InfoDot, PanelHelp, RefreshButton, Spinner, StatusPill } from "../../components";
import { useI18n } from "../../i18n";
import type {
  AsyncResource,
  LiveBrokerSpec,
  LiveConfigSnapshot,
  LiveLedgerEvent,
  LivePreflight,
  LiveRiskStatus,
  LiveRuntimeState,
} from "./types";
import { fmtMoney } from "./utils";

type Props = {
  cfg?: LiveConfigSnapshot;
  riskRows: Array<[string, string]>;
  onRefreshStatus: () => Promise<void>;
  runtimeMode: string;
  setRuntimeMode: Dispatch<SetStateAction<string>>;
  runtimeBroker: string;
  setRuntimeBroker: Dispatch<SetStateAction<string>>;
  selectedRuntimeBroker: string;
  liveBrokerOptions: LiveBrokerSpec[];
  selectedBrokerSpec?: LiveBrokerSpec;
  brokerCatalog: AsyncResource<LiveBrokerSpec[]>;
  preflight?: LivePreflight | null;
  preflightNetwork: boolean;
  setPreflightNetwork: Dispatch<SetStateAction<boolean>>;
  onCheckRuntime: () => void | Promise<unknown>;
  onConnectRuntime: () => void | Promise<unknown>;
  runtimeState: AsyncResource<LiveRuntimeState>;
  riskStatus: AsyncResource<LiveRiskStatus>;
};

export function LiveRuntimePanel({
  cfg,
  riskRows,
  onRefreshStatus,
  runtimeMode,
  setRuntimeMode,
  runtimeBroker,
  setRuntimeBroker,
  selectedRuntimeBroker,
  liveBrokerOptions,
  selectedBrokerSpec,
  brokerCatalog,
  preflight,
  preflightNetwork,
  setPreflightNetwork,
  onCheckRuntime,
  onConnectRuntime,
  runtimeState,
  riskStatus,
}: Props) {
  const { t } = useI18n();
  const runtimeSnapshot = runtimeState.data?.state;
  const runtimeEngine = runtimeSnapshot?.engine;
  const risk = riskStatus.data?.risk;
  const recovery = riskStatus.data?.recovery;
  const riskLimits = risk?.limits || {};
  const rejectionRows = riskStatus.data?.recent_rejections || [];

  return (
    <>
      <section className="panel">
        <div className="panel-head">
          <div className="panel-title-inline">
            <h2>{t("liveConfig")}</h2>
            <PanelHelp label={t("help")} title={t("liveConfig")} intro={t("liveConfigHelp")} items={[t("liveConfigHelp1"), t("liveConfigHelp2")]} />
          </div>
          <RefreshButton onClick={onRefreshStatus} />
        </div>
        {cfg ? (
          <div className="metric-grid compact">
            <div className="metric"><span className="metric-label">{t("liveMode")}</span><StatusPill status={cfg.mode} /></div>
            <div className="metric"><span className="metric-label">{t("liveBroker")}</span><strong>{cfg.broker}</strong></div>
            {riskRows.map(([label, value]) => (
              <div className="metric" key={label}><span className="metric-label">{label}</span><strong>{value}</strong></div>
            ))}
          </div>
        ) : (
          <Spinner />
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <div className="panel-title-inline">
            <h2>{t("liveRuntime")}</h2>
            <InfoDot tip={t("liveRuntimeNote")} />
          </div>
          <RefreshButton onClick={runtimeState.refresh} />
        </div>
        <div className="toolbar live-status-bar">
          <label className="field">
            <span>{t("liveMode")}</span>
            <select value={runtimeMode} onChange={(e) => setRuntimeMode(e.target.value)}>
              <option value="live">live</option>
              <option value="paper">paper</option>
              <option value="dry_run">dry_run</option>
            </select>
          </label>
          <label className="field">
            <span>{t("liveBroker")}</span>
            <select value={runtimeMode === "live" ? runtimeBroker : "paper"} onChange={(e) => setRuntimeBroker(e.target.value)} disabled={runtimeMode !== "live"}>
              {runtimeMode !== "live" ? <option value="paper">paper</option> : null}
              {runtimeMode === "live" && !liveBrokerOptions.length ? <option value="">{brokerCatalog.loading ? t("loading") : t("empty")}</option> : null}
              {runtimeMode === "live" ? liveBrokerOptions.map((broker) => (
                <option value={broker.name} key={broker.name}>{broker.name} - {broker.description || broker.gateway}</option>
              )) : null}
            </select>
          </label>
          <label className="inline-check compact">
            <input type="checkbox" checked={preflightNetwork} onChange={(e) => setPreflightNetwork(e.target.checked)} />
            <span>{t("liveNetworkCheck")}</span>
          </label>
          <div className="row-actions">
            <AsyncButton onClick={onCheckRuntime} disabled={runtimeMode === "live" && !selectedRuntimeBroker}>{t("livePreflight")}</AsyncButton>
            <AsyncButton className="button ghost" onClick={onConnectRuntime} disabled={runtimeMode === "live" && !selectedRuntimeBroker}>{t("liveRuntimeConnect")}</AsyncButton>
          </div>
        </div>
        {brokerCatalog.error ? <Alert tone="error">{brokerCatalog.error}</Alert> : null}
        {selectedBrokerSpec ? (
          <div className="metric-grid compact">
            <div className="metric"><span className="metric-label">{t("liveBrokerStatus")}</span><StatusPill status={selectedBrokerSpec.gateway_importable ? "importable" : "missing"} /></div>
            <div className="metric"><span className="metric-label">{t("liveMissingEnv")}</span><strong>{selectedBrokerSpec.missing_env.length}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveCapabilities")}</span><strong>{Object.values(selectedBrokerSpec.capabilities).filter((item) => item === true).length}</strong></div>
            <div className="metric wide"><span className="metric-label">{t("liveDescription")}</span><strong>{selectedBrokerSpec.description || selectedBrokerSpec.gateway}</strong></div>
          </div>
        ) : null}
        {preflight ? (
          <div className="stack">
            <div className="metric-grid compact">
              <div className="metric"><span className="metric-label">{t("liveBroker")}</span><strong>{preflight.broker}</strong></div>
              <div className="metric"><span className="metric-label">{t("status")}</span><StatusPill status={preflight.ok ? "ok" : "blocked"} /></div>
              <div className="metric"><span className="metric-label">{t("liveGateway")}</span><StatusPill status={preflight.gateway_importable ? "ok" : "missing"} /></div>
              <div className="metric"><span className="metric-label">{t("liveMissingEnv")}</span><strong>{preflight.missing_env.length}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveEndpoints")}</span><strong>{preflight.endpoints.filter((e) => e.ok).length}/{preflight.endpoints.length}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveNetworkCheck")}</span><StatusPill status={preflight.network_checked ? "checked" : "skipped"} /></div>
            </div>
            {preflight.description ? <Alert>{preflight.description}</Alert> : null}
            {preflight.missing_env.length ? <Alert tone="info">{preflight.missing_env.join(", ")}</Alert> : null}
            {preflight.endpoints.length ? (
              <DataTable<{ name: string; host: string; port: number; ok: boolean; detail: string }>
                rows={preflight.endpoints}
                empty={t("empty")}
                columns={[
                  { key: "name", label: t("name") },
                  { key: "host", label: t("host"), ellipsis: true },
                  { key: "port", label: t("port"), align: "right" },
                  { key: "ok", label: t("status"), render: (r) => <StatusPill status={r.ok ? "ok" : "blocked"} /> },
                  { key: "detail", label: t("result"), ellipsis: true },
                ]}
              />
            ) : null}
          </div>
        ) : null}
        {runtimeState.error ? <Alert tone="error">{runtimeState.error}</Alert> : null}
        {runtimeState.loading ? (
          <Spinner />
        ) : runtimeState.data?.exists && runtimeSnapshot && runtimeEngine ? (
          <div className="metric-grid compact">
            <div className="metric"><span className="metric-label">{t("liveModeState")}</span><StatusPill status={runtimeEngine.mode} /></div>
            <div className="metric"><span className="metric-label">{t("liveConnection")}</span><StatusPill status={runtimeEngine.connection} /></div>
            <div className="metric"><span className="metric-label">{t("liveBuyingPower")}</span><strong>{fmtMoney(runtimeEngine.buying_power)}</strong></div>
            <div className="metric"><span className="metric-label">{t("livePositionsCount")}</span><strong>{runtimeEngine.positions}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveContracts")}</span><strong>{runtimeEngine.contracts ?? 0}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveActiveOrders")}</span><strong>{runtimeEngine.active_orders}</strong></div>
            <div className="metric wide"><span className="metric-label">{t("liveStateFile")}</span><strong>{runtimeState.data?.state_path || ""}</strong></div>
          </div>
        ) : (
          <Alert>{t("liveRuntimeNoState")}</Alert>
        )}

        <h3>{t("liveRiskRecovery")}</h3>
        {riskStatus.error ? <Alert tone="error">{riskStatus.error}</Alert> : null}
        {riskStatus.loading ? (
          <Spinner />
        ) : (
          <div className="stack">
            <div className="metric-grid compact">
              <div className="metric"><span className="metric-label">{t("liveRiskOrdersToday")}</span><strong>{risk?.orders_today ?? 0}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRiskValueToday")}</span><strong>{fmtMoney(risk?.value_today ?? 0)}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRiskSeenRefs")}</span><strong>{risk?.seen_refs?.length ?? 0}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRecovery")}</span><StatusPill status={recovery?.risk_restored ? "restored" : "pending"} /></div>
              <div className="metric"><span className="metric-label">{t("liveRecoveryWarnings")}</span><strong>{recovery?.warnings?.length ?? 0}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRecentRejections")}</span><strong>{rejectionRows.length}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRiskMaxOrder")}</span><strong>{fmtMoney(Number(riskLimits.max_order_value ?? 0))}</strong></div>
              <div className="metric"><span className="metric-label">{t("liveRiskLot")}</span><strong>{String(riskLimits.lot_size ?? "-")}</strong></div>
            </div>
            {recovery?.warnings?.length ? (
              <Alert tone="info">{recovery.warnings.map((item) => item.kind || item.detail || "").filter(Boolean).join(", ")}</Alert>
            ) : null}
            <DataTable<LiveLedgerEvent>
              rows={rejectionRows}
              empty={t("empty")}
              columns={[
                { key: "ts", label: t("time"), ellipsis: true },
                { key: "kind", label: t("kind") },
                { key: "reference", label: t("liveReference"), ellipsis: true },
                { key: "payload", label: t("reason"), ellipsis: true, render: (r) => String(r.payload?.reason || r.payload?.rule || "") },
              ]}
            />
          </div>
        )}
      </section>
    </>
  );
}
