import type { Dispatch, SetStateAction } from "react";
import { Alert, AsyncButton, StatusPill } from "../../components";
import { useI18n } from "../../i18n";
import type { LiveDaemonStatus } from "./types";

type Props = {
  daemon?: LiveDaemonStatus | null;
  strategy: string;
  setStrategy: Dispatch<SetStateAction<string>>;
  strategyNames: string[];
  onStrategyStart: () => void | Promise<unknown>;
  onStrategyPause: () => void | Promise<unknown>;
  onStrategyReconcile: () => void | Promise<unknown>;
  onStrategyResume: () => void | Promise<unknown>;
  onStrategyStop: () => void | Promise<unknown>;
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
      {!props.strategyNames.length ? <Alert tone="info">{t("liveNoValidatedStrategies")}</Alert> : null}
      <div className="live-form-grid">
        <label className="field live-field-wide"><span>{t("liveTimingStrategy")}</span>
          <select value={props.strategy} onChange={(event) => props.setStrategy(event.target.value)}>
            <option value="">{t("liveSelectStrategy")}</option>
            {props.strategyNames.map((name) => <option value={name} key={name}>{name}</option>)}
          </select>
        </label>
      </div>
      <div className="live-runner-summary">
        <span><small>{t("liveRunnerState")}</small><StatusPill status={runnerState} /></span>
        <span><small>{t("liveRunnerPending")}</small><strong>{(runner?.pending_requests ?? 0) + (runner?.pending_intents ?? 0)}</strong></span>
        <span><small>{t("liveRunnerAlgo")}</small><StatusPill status={runner?.algo_armed ? "armed" : "idle"} /></span>
      </div>
      <div className="row-actions live-primary-actions">
        <AsyncButton onClick={props.onStrategyStart} disabled={!props.daemon?.running || !props.strategy.trim() || Boolean(runner?.active)}>{t("liveStrategyStart")}</AsyncButton>
        <AsyncButton className="button ghost" onClick={props.onStrategyPause} disabled={!props.daemon?.running || !runner?.active}>{t("liveStrategyPause")}</AsyncButton>
        <AsyncButton className="button ghost" onClick={props.onStrategyReconcile} disabled={!props.daemon?.running || !runner?.reconcile_required}>对账</AsyncButton>
        <AsyncButton className="button ghost" onClick={props.onStrategyResume} disabled={!props.daemon?.running || !runner?.paused}>{t("liveStrategyResume")}</AsyncButton>
        <AsyncButton className="button danger" onClick={props.onStrategyStop} disabled={!props.daemon?.running || !runner?.enabled}>{t("liveStrategyStop")}</AsyncButton>
      </div>
    </section>
  );
}
