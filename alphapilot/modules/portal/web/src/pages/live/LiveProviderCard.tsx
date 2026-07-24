import type { Dispatch, SetStateAction } from "react";
import { Alert, AsyncButton, StatusPill } from "../../components";
import { useI18n } from "../../i18n";
import type {
  LiveBrokerSpec,
  LiveDaemonStatus,
  LivePreflight,
  LiveQuoteProviderSpec,
} from "./types";

type Workspace = "live" | "shadow" | "simulation" | "paper";

type Props = {
  workspace: Workspace;
  daemon?: LiveDaemonStatus | null;
  brokers: LiveBrokerSpec[];
  quoteProviders: LiveQuoteProviderSpec[];
  runtimeBroker: string;
  setRuntimeBroker: Dispatch<SetStateAction<string>>;
  runtimeQuoteProvider: string;
  setRuntimeQuoteProvider: Dispatch<SetStateAction<string>>;
  symbols: string;
  setSymbols: Dispatch<SetStateAction<string>>;
  initialCash: string;
  setInitialCash: Dispatch<SetStateAction<string>>;
  providerReady: boolean;
  providerSelectionLocked: boolean;
  canStartDaemon: boolean;
  preflight: LivePreflight | null;
  preflightNetwork: boolean;
  setPreflightNetwork: Dispatch<SetStateAction<boolean>>;
  onPreflight: () => void | Promise<unknown>;
  onConnect: () => void | Promise<unknown>;
  onStartDaemon: () => void | Promise<unknown>;
  onRefreshDaemon: () => void | Promise<unknown>;
  onReconnectDaemon: () => void | Promise<unknown>;
  onStopDaemon: () => void | Promise<unknown>;
};

export function LiveProviderCard(props: Props) {
  const { t } = useI18n();
  const selectedBroker = props.workspace === "paper"
    ? undefined
    : props.brokers.find((item) => item.name === props.runtimeBroker);
  const selectedQuote = props.workspace === "paper"
    ? undefined
    : props.quoteProviders.find((item) => item.name === props.runtimeQuoteProvider);
  const missingEnv = Array.from(new Set([
    ...(selectedBroker?.missing_env || []),
    ...(selectedQuote?.missing_env || []),
  ]));

  return (
    <section className="panel live-provider-card" aria-labelledby="live-provider-title">
      <div className="panel-head">
        <div>
          <h2 id="live-provider-title">{t("liveProviderWorkspace")}</h2>
          <span className="muted">{t("liveProviderWorkspaceHint")}</span>
        </div>
        <StatusPill status={props.daemon?.running ? "running" : "stopped"} />
      </div>

      <div className="live-form-grid">
        {props.workspace === "paper" ? (
          <>
            <label className="field">
              <span>{t("liveTradeBroker")}</span>
              <select aria-label={t("liveTradeBroker")} value="paper" disabled>
                <option value="paper">paper</option>
              </select>
            </label>
            <label className="field">
              <span>{t("liveQuoteProvider")}</span>
              <select aria-label={t("liveQuoteProvider")} value="paper" disabled>
                <option value="paper">paper (synthetic)</option>
              </select>
            </label>
          </>
        ) : (
          <>
            <label className="field">
              <span>{t("liveTradeBroker")}</span>
              <select
                aria-label={t("liveTradeBroker")}
                value={props.runtimeBroker}
                onChange={(event) => props.setRuntimeBroker(event.target.value)}
                disabled={props.providerSelectionLocked}
              >
                {props.brokers.map((item) => (
                  <option value={item.name} disabled={!item.gateway_importable} key={item.name}>
                    {item.name} - {item.description}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>{t("liveQuoteProvider")}</span>
              <select
                aria-label={t("liveQuoteProvider")}
                value={props.runtimeQuoteProvider}
                onChange={(event) => props.setRuntimeQuoteProvider(event.target.value)}
                disabled={props.providerSelectionLocked}
              >
                {props.quoteProviders.map((item) => (
                  <option value={item.name} disabled={!item.gateway_importable} key={item.name}>
                    {item.name} ({item.data_kind}) - {item.description}
                  </option>
                ))}
              </select>
            </label>
          </>
        )}
        <label className="field live-field-wide">
          <span>{t("liveSymbols")}</span>
          <input
            value={props.symbols}
            onChange={(event) => props.setSymbols(event.target.value)}
            placeholder="600000, 000001"
            disabled={props.providerSelectionLocked}
          />
        </label>
        {props.workspace === "paper" && !props.daemon?.alive ? (
          <label className="field">
            <span>{t("liveInitCash")}</span>
            <input
              value={props.initialCash}
              onChange={(event) => props.setInitialCash(event.target.value)}
              inputMode="decimal"
            />
          </label>
        ) : null}
      </div>

      {props.workspace === "paper" ? <Alert tone="info">{t("livePaperDaemonHint")}</Alert> : null}
      {props.providerSelectionLocked ? <Alert tone="info">{t("liveProviderLocked")}</Alert> : null}
      {!props.providerSelectionLocked && missingEnv.length ? (
        <Alert tone="error">{t("liveMissingEnv")}: {missingEnv.join(", ")}</Alert>
      ) : null}

      <div className="live-runner-summary">
        <span>
          <small>{t("liveTradeChannel")}</small>
          <strong>{props.workspace === "paper" ? "paper" : selectedBroker?.name || "—"}</strong>
        </span>
        <span>
          <small>{t("liveQuoteChannel")}</small>
          <strong>{props.workspace === "paper" ? "paper / synthetic" : `${selectedQuote?.name || "—"} / ${selectedQuote?.data_kind || "—"}`}</strong>
        </span>
        <span>
          <small>{t("livePluginPackage")}</small>
          <strong>{props.workspace === "paper" ? "alphapilot-core" : selectedBroker?.distribution || selectedBroker?.plugin_id || selectedQuote?.distribution || selectedQuote?.plugin_id || "—"}</strong>
        </span>
      </div>

      <label className="inline-check compact">
        <input
          aria-label={t("liveNetworkCheck")}
          type="checkbox"
          checked={props.preflightNetwork}
          onChange={(event) => props.setPreflightNetwork(event.target.checked)}
        />
        <span>{t("liveNetworkCheck")}</span>
      </label>
      <div className="row-actions live-primary-actions">
        <AsyncButton className="button small" onClick={props.onPreflight} disabled={!props.providerReady}>
          {t("livePreflight")}
        </AsyncButton>
        <AsyncButton
          className="button ghost small"
          onClick={props.onConnect}
          disabled={props.workspace === "paper" || !props.providerReady}
        >
          {t("liveRuntimeConnect")}
        </AsyncButton>
        {!props.daemon?.alive ? (
          <AsyncButton onClick={props.onStartDaemon} disabled={!props.canStartDaemon}>
            {t("liveDaemonStart")}
          </AsyncButton>
        ) : (
          <>
            <AsyncButton className="button ghost small" onClick={props.onRefreshDaemon} disabled={!props.daemon.running}>
              {t("liveDaemonRefresh")}
            </AsyncButton>
            <AsyncButton className="button ghost small" onClick={props.onReconnectDaemon} disabled={!props.daemon.running}>
              {t("liveDaemonReconnect")}
            </AsyncButton>
            <AsyncButton className="button danger small" onClick={props.onStopDaemon}>
              {t("liveDaemonStop")}
            </AsyncButton>
          </>
        )}
      </div>

      {props.preflight ? (
        <div className="live-diagnostic-summary" role="status">
          <span>{t("livePreflight")}</span>
          <StatusPill status={props.preflight.ok ? "ok" : "blocked"} />
          <strong>{props.preflight.missing_env.length ? props.preflight.missing_env.join(", ") : t("liveNoMissingEnv")}</strong>
        </div>
      ) : null}
    </section>
  );
}
