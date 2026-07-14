import type { Dispatch, SetStateAction } from "react";
import { AsyncButton, StatusPill } from "../../components";
import { useI18n } from "../../i18n";
import type { LiveDaemonStatus } from "./types";

type Props = {
  daemon?: LiveDaemonStatus | null;
  symbols: string;
  setSymbols: Dispatch<SetStateAction<string>>;
  strategy: string;
  setStrategy: Dispatch<SetStateAction<string>>;
  strategyNames: string[];
  initialCash: string;
  setInitialCash: Dispatch<SetStateAction<string>>;
  simulated: boolean;
  canStartDaemon: boolean;
  onStartDaemon: () => void | Promise<unknown>;
  onStrategyStart: () => void | Promise<unknown>;
  onStrategyPause: () => void | Promise<unknown>;
  onStrategyReconcile: () => void | Promise<unknown>;
  onStrategyResume: () => void | Promise<unknown>;
  onStrategyStop: () => void | Promise<unknown>;
  onRefreshDaemon: () => void | Promise<unknown>;
  onReconnectDaemon: () => void | Promise<unknown>;
  onStopDaemon: () => void | Promise<unknown>;
};

export function LiveStrategyCard(props: Props) {
  const { t } = useI18n();
  const runner = props.daemon?.runner_status;
  const runnerState = runner?.active ? "active" : runner?.paused ? "paused" : runner?.stopped ? "stopped" : runner?.enabled ? "idle" : "disabled";

  return (
    <section className="panel live-work-card" aria-labelledby="live-strategy-title">
      <div className="panel-head">
        <div>
          <h2 id="live-strategy-title">{t("liveStrategyWorkspace")}</h2>
          <span className="muted">{t("liveStrategyWorkspaceHint")}</span>
        </div>
        <StatusPill status={runnerState} />
      </div>
      <div className="live-form-grid">
        <label className="field live-field-wide"><span>{t("liveSymbols")}</span><input value={props.symbols} onChange={(event) => props.setSymbols(event.target.value)} placeholder="600000, 000001" /></label>
        <label className="field"><span>{t("liveTimingStrategy")}</span>
          <select value={props.strategy} onChange={(event) => props.setStrategy(event.target.value)}>
            <option value="">{t("liveSelectStrategy")}</option>
            {props.strategyNames.map((name) => <option value={name} key={name}>{name}</option>)}
          </select>
        </label>
        {props.simulated && !props.daemon?.alive ? (
          <label className="field"><span>{t("liveInitCash")}</span><input value={props.initialCash} onChange={(event) => props.setInitialCash(event.target.value)} inputMode="decimal" /></label>
        ) : null}
      </div>
      <div className="live-runner-summary">
        <span><small>{t("liveRunnerState")}</small><StatusPill status={runnerState} /></span>
        <span><small>{t("liveRunnerPending")}</small><strong>{(runner?.pending_requests ?? 0) + (runner?.pending_intents ?? 0)}</strong></span>
        <span><small>{t("liveRunnerAlgo")}</small><StatusPill status={runner?.algo_armed ? "armed" : "idle"} /></span>
      </div>
      <div className="row-actions live-primary-actions">
        {!props.daemon?.alive ? <AsyncButton onClick={props.onStartDaemon} disabled={!props.canStartDaemon}>{t("liveDaemonStart")}</AsyncButton> : null}
        <AsyncButton onClick={props.onStrategyStart} disabled={!props.strategy.trim() || Boolean(runner?.active)}>{t("liveStrategyStart")}</AsyncButton>
        <AsyncButton className="button ghost" onClick={props.onStrategyPause} disabled={!props.daemon?.running || !runner?.active}>{t("liveStrategyPause")}</AsyncButton>
        <AsyncButton className="button ghost" onClick={props.onStrategyReconcile} disabled={!props.daemon?.running || !runner?.reconcile_required}>对账</AsyncButton>
        <AsyncButton className="button ghost" onClick={props.onStrategyResume} disabled={!props.daemon?.running || !runner?.paused}>{t("liveStrategyResume")}</AsyncButton>
        <AsyncButton className="button danger" onClick={props.onStrategyStop} disabled={!props.daemon?.running || !runner?.enabled}>{t("liveStrategyStop")}</AsyncButton>
      </div>
      {props.daemon?.alive ? (
        <details className="live-more-actions">
          <summary>{t("liveTechnicalActions")}</summary>
          <div className="row-actions">
            <AsyncButton className="button ghost small" onClick={props.onRefreshDaemon} disabled={!props.daemon.running}>{t("liveDaemonRefresh")}</AsyncButton>
            <AsyncButton className="button ghost small" onClick={props.onReconnectDaemon} disabled={!props.daemon.running}>{t("liveDaemonReconnect")}</AsyncButton>
            <AsyncButton className="button ghost small" onClick={props.onStopDaemon}>{t("liveDaemonStop")}</AsyncButton>
          </div>
        </details>
      ) : null}
    </section>
  );
}
