import type { Dispatch, SetStateAction } from "react";
import { Alert, AsyncButton, DataTable, Spinner, StatusPill } from "../../components";
import { useI18n } from "../../i18n";
import type {
  AsyncResource,
  JsonInputState,
  LiveCommandStatus,
  LiveDaemonStatus,
  LiveOrder,
  LivePosition,
  LiveTrade,
} from "./types";
import { fmtMoney } from "./utils";

type Props = {
  daemonStatus: AsyncResource<LiveDaemonStatus>;
  daemonSymbols: string;
  setDaemonSymbols: Dispatch<SetStateAction<string>>;
  daemonTimingStrategy: string;
  setDaemonTimingStrategy: Dispatch<SetStateAction<string>>;
  daemonTimingFreq: string;
  setDaemonTimingFreq: Dispatch<SetStateAction<string>>;
  daemonTimingParams: JsonInputState;
  daemonOrderCode: string;
  setDaemonOrderCode: Dispatch<SetStateAction<string>>;
  daemonOrderSide: string;
  setDaemonOrderSide: Dispatch<SetStateAction<string>>;
  daemonOrderVol: string;
  setDaemonOrderVol: Dispatch<SetStateAction<string>>;
  daemonOrderPrice: string;
  setDaemonOrderPrice: Dispatch<SetStateAction<string>>;
  daemonOrderType: string;
  setDaemonOrderType: Dispatch<SetStateAction<string>>;
  daemonOrderRef: string;
  setDaemonOrderRef: Dispatch<SetStateAction<string>>;
  daemonTargetJson: JsonInputState;
  daemonTargetRoute: boolean;
  setDaemonTargetRoute: Dispatch<SetStateAction<boolean>>;
  onStrategyStatus: () => void | Promise<unknown>;
  onStrategyStart: () => void | Promise<unknown>;
  onStrategyPause: () => void | Promise<unknown>;
  onStrategyResume: () => void | Promise<unknown>;
  onStrategyStop: () => void | Promise<unknown>;
  onStartDaemon: () => void | Promise<unknown>;
  onStopDaemon: () => void | Promise<unknown>;
  onHaltDaemon: () => void | Promise<unknown>;
  onResumeDaemon: () => void | Promise<unknown>;
  onRefreshDaemon: () => void | Promise<unknown>;
  onReconnectDaemon: () => void | Promise<unknown>;
  onCancelDaemonOrder: (order: LiveOrder) => void | Promise<unknown>;
  onSubmitDaemonOrder: () => void | Promise<unknown>;
  onSubmitDaemonTarget: () => void | Promise<unknown>;
};

export function LiveDaemonPanel({
  daemonStatus,
  daemonSymbols,
  setDaemonSymbols,
  daemonTimingStrategy,
  setDaemonTimingStrategy,
  daemonTimingFreq,
  setDaemonTimingFreq,
  daemonTimingParams,
  daemonOrderCode,
  setDaemonOrderCode,
  daemonOrderSide,
  setDaemonOrderSide,
  daemonOrderVol,
  setDaemonOrderVol,
  daemonOrderPrice,
  setDaemonOrderPrice,
  daemonOrderType,
  setDaemonOrderType,
  daemonOrderRef,
  setDaemonOrderRef,
  daemonTargetJson,
  daemonTargetRoute,
  setDaemonTargetRoute,
  onStrategyStatus,
  onStrategyStart,
  onStrategyPause,
  onStrategyResume,
  onStrategyStop,
  onStartDaemon,
  onStopDaemon,
  onHaltDaemon,
  onResumeDaemon,
  onRefreshDaemon,
  onReconnectDaemon,
  onCancelDaemonOrder,
  onSubmitDaemonOrder,
  onSubmitDaemonTarget,
}: Props) {
  const { t } = useI18n();
  const daemon = daemonStatus.data;
  const daemonEngine = daemon?.state?.engine;
  const runnerStatus = daemon?.runner_status;
  const daemonAccount = daemon?.state?.account;
  const daemonOrders = daemon?.state?.orders || [];
  const daemonPositions = daemon?.state?.positions || [];
  const daemonTrades = daemon?.state?.trades || [];
  const commandRows = daemon?.command_status_tail || [];

  return (
    <section className="panel">
      <h3>{t("liveDaemon")}</h3>
      {daemonStatus.error ? <Alert tone="error">{daemonStatus.error}</Alert> : null}
      {daemonStatus.loading ? (
        <Spinner />
      ) : (
        <div className="stack">
          <div className="toolbar live-status-bar">
            <label className="field">
              <span>{t("liveSymbols")}</span>
              <input value={daemonSymbols} onChange={(e) => setDaemonSymbols(e.target.value)} />
            </label>
            <label className="field">
              <span>{t("liveTimingStrategy")}</span>
              <input value={daemonTimingStrategy} onChange={(e) => setDaemonTimingStrategy(e.target.value)} placeholder="sma_filter" />
            </label>
            <label className="field">
              <span>{t("liveTimingFreq")}</span>
              <select value={daemonTimingFreq} onChange={(e) => setDaemonTimingFreq(e.target.value)}>
                <option value="day">day</option>
                <option value="min">min</option>
              </select>
            </label>
          </div>
          <div className="field">
            <span>{t("liveTimingParams")}</span>
            <textarea rows={3} value={daemonTimingParams.raw} onChange={(e) => daemonTimingParams.setRaw(e.target.value)} spellCheck={false} />
          </div>
          <div className="metric-grid compact">
            <div className="metric"><span className="metric-label">PID</span><strong>{daemon?.pid || "-"}</strong></div>
            <div className="metric"><span className="metric-label">{t("status")}</span><StatusPill status={daemon?.status || (daemon?.running ? "running" : "stopped")} /></div>
            <div className="metric"><span className="metric-label">{t("liveConnection")}</span><StatusPill status={daemonEngine?.connection || "-"} /></div>
            <div className="metric"><span className="metric-label">{t("liveKillState")}</span><StatusPill status={daemonEngine?.halted ? "halted" : "running"} /></div>
            <div className="metric"><span className="metric-label">{t("liveBalance")}</span><strong>{fmtMoney(daemonAccount?.balance ?? 0)}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveBuyingPower")}</span><strong>{fmtMoney(daemonEngine?.buying_power ?? daemonAccount?.available ?? 0)}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveFrozen")}</span><strong>{fmtMoney(daemonAccount?.frozen ?? 0)}</strong></div>
            <div className="metric"><span className="metric-label">{t("livePositionsCount")}</span><strong>{daemonEngine?.positions ?? "-"}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveContracts")}</span><strong>{daemonEngine?.contracts ?? "-"}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveCommands")}</span><strong>{daemon?.commands_processed ?? 0}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveLastCommand")}</span><strong>{daemon?.last_command?.action || "-"}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveTimingStrategy")}</span><strong>{daemon?.runner?.strategy || "-"}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveRunnerState")}</span><StatusPill status={runnerStatus?.active ? "active" : runnerStatus?.paused ? "paused" : runnerStatus?.stopped ? "stopped" : runnerStatus?.enabled ? "idle" : "disabled"} /></div>
            <div className="metric"><span className="metric-label">{t("liveRunnerPending")}</span><strong>{runnerStatus?.pending_requests ?? 0}</strong></div>
            <div className="metric"><span className="metric-label">{t("liveRunnerAlgo")}</span><StatusPill status={runnerStatus?.algo_armed ? "armed" : "idle"} /></div>
          </div>
          <div className="toolbar live-status-bar">
            <div className="row-actions">
              <AsyncButton className="button ghost" onClick={onStrategyStatus} disabled={!daemon?.running}>{t("liveStrategyStatus")}</AsyncButton>
              <AsyncButton onClick={onStrategyStart} disabled={!daemon?.running || !daemonTimingStrategy.trim() || Boolean(runnerStatus?.active)}>{t("liveStrategyStart")}</AsyncButton>
              <AsyncButton className="button ghost" onClick={onStrategyPause} disabled={!daemon?.running || !runnerStatus?.active}>{t("liveStrategyPause")}</AsyncButton>
              <AsyncButton className="button ghost" onClick={onStrategyResume} disabled={!daemon?.running || !runnerStatus?.paused}>{t("liveStrategyResume")}</AsyncButton>
              <AsyncButton className="button danger" onClick={onStrategyStop} disabled={!daemon?.running || !runnerStatus?.enabled}>{t("liveStrategyStop")}</AsyncButton>
            </div>
          </div>
          <div className="toolbar live-status-bar">
            <div className="row-actions">
              <AsyncButton onClick={onStartDaemon} disabled={Boolean(daemon?.alive)}>{t("liveDaemonStart")}</AsyncButton>
              <AsyncButton className="button ghost" onClick={onRefreshDaemon} disabled={!daemon?.running}>{t("liveDaemonRefresh")}</AsyncButton>
              <AsyncButton className="button ghost" onClick={onReconnectDaemon} disabled={!daemon?.running}>{t("liveDaemonReconnect")}</AsyncButton>
              {daemonEngine?.halted ? (
                <AsyncButton onClick={onResumeDaemon} disabled={!daemon?.running}>{t("liveResume")}</AsyncButton>
              ) : (
                <AsyncButton className="button danger" onClick={onHaltDaemon} disabled={!daemon?.running}>{t("liveHalt")}</AsyncButton>
              )}
              <AsyncButton className="button ghost" onClick={onStopDaemon} disabled={!daemon?.alive}>{t("liveDaemonStop")}</AsyncButton>
            </div>
          </div>
          <h3>{t("liveDaemonOrderTicket")}</h3>
          <div className="toolbar live-status-bar">
            <label className="field"><span>{t("liveCode")}</span><input value={daemonOrderCode} onChange={(e) => setDaemonOrderCode(e.target.value)} /></label>
            <label className="field">
              <span>{t("liveSideCol")}</span>
              <select value={daemonOrderSide} onChange={(e) => setDaemonOrderSide(e.target.value)}>
                <option value="buy">{t("liveBuy")}</option>
                <option value="sell">{t("liveSell")}</option>
              </select>
            </label>
            <label className="field"><span>{t("liveVolume")}</span><input value={daemonOrderVol} onChange={(e) => setDaemonOrderVol(e.target.value)} inputMode="numeric" /></label>
            <label className="field"><span>{t("livePrice")}</span><input value={daemonOrderPrice} onChange={(e) => setDaemonOrderPrice(e.target.value)} inputMode="decimal" /></label>
            <label className="field">
              <span>{t("liveOrderType")}</span>
              <select value={daemonOrderType} onChange={(e) => setDaemonOrderType(e.target.value)}>
                <option value="limit">limit</option>
                <option value="market">market</option>
              </select>
            </label>
            <label className="field"><span>{t("liveReference")}</span><input value={daemonOrderRef} onChange={(e) => setDaemonOrderRef(e.target.value)} /></label>
            <div className="row-actions">
              <AsyncButton onClick={onSubmitDaemonOrder} disabled={!daemon?.running || daemonEngine?.halted || !daemonOrderCode.trim()}>{t("liveSubmitOrder")}</AsyncButton>
            </div>
          </div>
          <h3>{t("liveDaemonTargetTicket")}</h3>
          <div className="field">
            <span>{t("liveSubmitTarget")}</span>
            <textarea rows={5} value={daemonTargetJson.raw} onChange={(e) => daemonTargetJson.setRaw(e.target.value)} spellCheck={false} />
            <small className="field-hint">{t("liveDaemonTargetHint")}</small>
          </div>
          <div className="toolbar live-status-bar">
            <label className="inline-check compact">
              <input type="checkbox" checked={daemonTargetRoute} onChange={(e) => setDaemonTargetRoute(e.target.checked)} />
              <span>{t("liveRouteTarget")}</span>
            </label>
            <div className="row-actions">
              <AsyncButton onClick={onSubmitDaemonTarget} disabled={!daemon?.running || (daemonTargetRoute && Boolean(daemonEngine?.halted))}>
                {daemonTargetRoute ? t("liveSubmit") : t("livePlanTarget")}
              </AsyncButton>
            </div>
          </div>
          <h3>{t("liveOrders")}</h3>
          <DataTable<LiveOrder>
            rows={daemonOrders.slice(-8).reverse()}
            empty={t("empty")}
            columns={[
              { key: "order_id", label: t("liveOrderId"), ellipsis: true },
              { key: "code", label: t("liveCode") },
              { key: "side", label: t("liveSideCol"), render: (r) => t(r.side === "buy" ? "liveBuy" : "liveSell") },
              { key: "price", label: t("livePrice"), align: "right", render: (r) => fmtMoney(r.price) },
              { key: "volume", label: t("liveVolume"), align: "right" },
              { key: "traded", label: t("liveTraded"), align: "right" },
              { key: "status", label: t("status"), render: (r) => <StatusPill status={r.status} /> },
              {
                key: "action",
                label: t("action"),
                render: (r) => r.active ? (
                  <AsyncButton className="button small danger" onClick={() => onCancelDaemonOrder(r)}>{t("liveCancelOrder")}</AsyncButton>
                ) : "—",
              },
            ]}
          />
          <h3>{t("livePositions")}</h3>
          <DataTable<LivePosition>
            rows={daemonPositions}
            empty={t("empty")}
            columns={[
              { key: "code", label: t("liveCode") },
              { key: "exchange", label: t("liveExchange") },
              { key: "volume", label: t("liveVolume"), align: "right" },
              { key: "available", label: t("liveAvailable"), align: "right" },
              { key: "price", label: t("liveAvgPrice"), align: "right", render: (r) => fmtMoney(r.price) },
            ]}
          />
          <h3>{t("liveTrades")}</h3>
          <DataTable<LiveTrade>
            rows={daemonTrades.slice(-8).reverse()}
            empty={t("empty")}
            columns={[
              { key: "trade_id", label: t("tradeId"), ellipsis: true },
              { key: "code", label: t("liveCode") },
              { key: "side", label: t("liveSideCol"), render: (r) => t(r.side === "buy" ? "liveBuy" : "liveSell") },
              { key: "price", label: t("livePrice"), align: "right", render: (r) => fmtMoney(r.price) },
              { key: "volume", label: t("liveVolume"), align: "right" },
            ]}
          />
          <h3>{t("liveCommands")}</h3>
          <DataTable<LiveCommandStatus>
            rows={commandRows.slice(-8).reverse()}
            empty={t("empty")}
            columns={[
              { key: "ts", label: t("time"), ellipsis: true },
              { key: "action", label: t("action") },
              { key: "stage", label: t("status"), render: (r) => <StatusPill status={String(r.stage || "")} /> },
              { key: "result", label: t("result"), ellipsis: true, render: (r) => String(r.result?.error || r.result?.message || r.result?.action || "") },
            ]}
          />
        </div>
      )}
    </section>
  );
}
