import { AsyncButton, RefreshButton, StatusPill } from "../../components";
import { useI18n } from "../../i18n";
import type { LiveDaemonStatus } from "./types";
import { fmtMoney } from "./utils";

type Props = {
  workspace: "live" | "shadow" | "simulation" | "paper";
  runtimeMode: string;
  tradeBroker: string;
  quoteProvider: string;
  daemon?: LiveDaemonStatus | null;
  onRefresh: () => void | Promise<unknown>;
  onOpenDiagnostics: () => void;
  onHalt: () => void | Promise<unknown>;
  onResume: () => void | Promise<unknown>;
};

function money(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? fmtMoney(parsed) : "—";
}

function ratio(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return `${(parsed <= 1 ? parsed * 100 : parsed).toFixed(2)}%`;
}

export function LiveStatusBar({
  workspace,
  runtimeMode,
  tradeBroker,
  quoteProvider,
  daemon,
  onRefresh,
  onOpenDiagnostics,
  onHalt,
  onResume,
}: Props) {
  const { t } = useI18n();
  const engine = daemon?.state?.engine;
  const account = daemon?.state?.account;
  const runner = daemon?.runner_status;
  const halted = Boolean(engine?.halted);

  return (
    <section className={`live-workspace-status is-${workspace}`} aria-label={t("liveWorkspaceStatus")}>
      <div className="live-status-context">
        <span className={`live-environment-badge ${workspace}`}>{workspace === "live" ? t("liveEnvironmentLive") : workspace === "shadow" ? "SHADOW" : workspace === "simulation" ? t("liveEnvironmentSimulation") : t("liveEnvironmentPaper")}</span>
        <span><small>{t("liveTradeBroker")}</small><strong>{tradeBroker || "—"}</strong></span>
        <span><small>{t("liveQuoteProvider")}</small><strong>{quoteProvider || "—"}</strong></span>
        <span><small>{t("liveConnection")}</small><StatusPill status={daemon?.running ? engine?.connection || "connected" : "disconnected"} /></span>
        <span><small>daemon</small><StatusPill status={daemon?.alive ? daemon.status || "running" : "stopped"} /></span>
        <span><small>{t("liveSession")}</small><StatusPill status={engine?.session || "closed"} /></span>
        <span><small>{t("liveTimingStrategy")}</small><strong>{daemon?.runner?.strategy || "—"}</strong></span>
        <span><small>{t("liveKillState")}</small><StatusPill status={halted ? "halted" : daemon?.running ? "ready" : "stopped"} /></span>
      </div>
      <div className="live-account-strip">
        <span><small>{t("liveBalance")}</small><strong>{money(account?.balance)}</strong></span>
        <span><small>{t("liveBuyingPower")}</small><strong>{money(account?.available ?? account?.buying_power)}</strong></span>
        <span><small>{t("liveFrozen")}</small><strong>{money(account?.frozen)}</strong></span>
        <span><small>{t("livePositionProfit")}</small><strong>{money(account?.position_profit)}</strong></span>
        <span><small>{t("liveCloseProfit")}</small><strong>{money(account?.close_profit)}</strong></span>
        <span><small>{t("liveCommission")}</small><strong>{money(account?.commission)}</strong></span>
        <span><small>{t("liveRiskRatio")}</small><strong>{ratio(account?.risk_ratio)}</strong></span>
      </div>
      <div className="live-status-actions">
        <RefreshButton onClick={onRefresh} iconOnly />
        <button type="button" className="button ghost small" onClick={onOpenDiagnostics}>{t("liveDiagnostics")}</button>
        {halted ? (
          <AsyncButton className="button small" onClick={onResume} disabled={!daemon?.running}>{t("liveResume")}</AsyncButton>
        ) : (
          <AsyncButton className="button danger small" onClick={onHalt} disabled={!daemon?.running}>{t("liveHalt")}</AsyncButton>
        )}
      </div>
      <span className="sr-only">{runtimeMode}</span>
      <span className="sr-only">{runner?.active ? "strategy-active" : "strategy-inactive"}</span>
    </section>
  );
}
