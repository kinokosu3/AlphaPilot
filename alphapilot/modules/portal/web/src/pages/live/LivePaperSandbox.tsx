import type { Dispatch, SetStateAction } from "react";
import { Alert, AsyncButton, DataTable, InfoDot, StatusPill } from "../../components";
import { useI18n } from "../../i18n";
import type { LiveOrder, LivePosition, LiveState, LiveTrade } from "./types";
import { fmtMoney } from "./utils";

type Props = {
  running: boolean;
  state?: LiveState;
  cash: string;
  setCash: Dispatch<SetStateAction<string>>;
  orderCode: string;
  setOrderCode: Dispatch<SetStateAction<string>>;
  orderSide: string;
  setOrderSide: Dispatch<SetStateAction<string>>;
  orderVol: string;
  setOrderVol: Dispatch<SetStateAction<string>>;
  orderPrice: string;
  setOrderPrice: Dispatch<SetStateAction<string>>;
  targetJson: string;
  setTargetJson: Dispatch<SetStateAction<string>>;
  onConnect: () => void | Promise<unknown>;
  onSubmitOrder: () => void | Promise<unknown>;
  onSubmitTarget: () => void | Promise<unknown>;
  onHalt: () => void | Promise<unknown>;
  onResume: () => void | Promise<unknown>;
  onReset: () => void | Promise<unknown>;
};

export function LivePaperSandbox({
  running,
  state,
  cash,
  setCash,
  orderCode,
  setOrderCode,
  orderSide,
  setOrderSide,
  orderVol,
  setOrderVol,
  orderPrice,
  setOrderPrice,
  targetJson,
  setTargetJson,
  onConnect,
  onSubmitOrder,
  onSubmitTarget,
  onHalt,
  onResume,
  onReset,
}: Props) {
  const { t } = useI18n();

  return (
    <>
      <Alert tone="info">{t("livePaperNote")}</Alert>

      <section className="panel">
        <div className="panel-head">
          <div className="panel-title-inline">
            <h2>{t("livePaper")}</h2>
            <InfoDot tip={t("livePaperNote")} />
          </div>
        </div>

        {!running || !state ? (
          <div className="toolbar">
            <label className="field">
              <span>{t("liveInitCash")}</span>
              <input value={cash} onChange={(e) => setCash(e.target.value)} inputMode="decimal" />
            </label>
            <AsyncButton onClick={onConnect}>{t("liveConnect")}</AsyncButton>
          </div>
        ) : (
          <div className="stack">
            <div className="toolbar live-status-bar">
              <span className="metric"><span className="metric-label">{t("liveModeState")}</span><StatusPill status={state.snapshot.mode} /></span>
              <span className="metric"><span className="metric-label">{t("liveKillState")}</span><StatusPill status={state.snapshot.halted ? "halted" : "running"} /></span>
              <span className="metric"><span className="metric-label">{t("liveSession")}</span><StatusPill status={state.snapshot.session} /></span>
              <span className="metric"><span className="metric-label">{t("liveConnection")}</span><StatusPill status={state.snapshot.connection} /></span>
              <span className="metric"><span className="metric-label">{t("liveBuyingPower")}</span><strong>{fmtMoney(state.account.buying_power)}</strong></span>
              <div className="row-actions">
                {state.snapshot.halted ? (
                  <AsyncButton onClick={onResume}>{t("liveResume")}</AsyncButton>
                ) : (
                  <AsyncButton className="button danger" onClick={onHalt}>{t("liveHalt")}</AsyncButton>
                )}
                <AsyncButton className="button ghost" onClick={onReset}>{t("liveReset")}</AsyncButton>
              </div>
            </div>

            <div className="toolbar">
              <input placeholder={t("liveCode")} value={orderCode} onChange={(e) => setOrderCode(e.target.value)} />
              <select value={orderSide} onChange={(e) => setOrderSide(e.target.value)}>
                <option value="buy">{t("liveBuy")}</option>
                <option value="sell">{t("liveSell")}</option>
              </select>
              <input placeholder={t("liveVolume")} value={orderVol} onChange={(e) => setOrderVol(e.target.value)} inputMode="numeric" />
              <input placeholder={t("livePrice")} value={orderPrice} onChange={(e) => setOrderPrice(e.target.value)} inputMode="decimal" />
              <AsyncButton onClick={onSubmitOrder}>{t("liveSubmitOrder")}</AsyncButton>
            </div>

            <div className="field">
              <span>{t("liveSubmitTarget")}</span>
              <textarea rows={5} value={targetJson} onChange={(e) => setTargetJson(e.target.value)} spellCheck={false} />
              <small className="field-hint">{t("liveTargetHint")}</small>
              <div className="row-actions"><AsyncButton onClick={onSubmitTarget}>{t("liveSubmit")}</AsyncButton></div>
            </div>

            <h3>{t("livePositions")}</h3>
            <DataTable<LivePosition>
              rows={state.positions}
              empty={t("empty")}
              columns={[
                { key: "code", label: t("liveCode") },
                { key: "volume", label: t("liveVolume"), align: "right" },
                { key: "available", label: t("liveAvailable"), align: "right" },
                { key: "price", label: t("liveAvgPrice"), align: "right", render: (r) => fmtMoney(r.price) },
              ]}
            />

            <h3>{t("liveOrders")}</h3>
            <DataTable<LiveOrder>
              rows={state.orders}
              empty={t("empty")}
              columns={[
                { key: "order_id", label: t("liveOrderId"), ellipsis: true },
                { key: "code", label: t("liveCode") },
                { key: "side", label: t("liveSideCol"), render: (r) => t(r.side === "buy" ? "liveBuy" : "liveSell") },
                { key: "price", label: t("livePrice"), align: "right", render: (r) => fmtMoney(r.price) },
                { key: "volume", label: t("liveVolume"), align: "right" },
                { key: "traded", label: t("liveTraded"), align: "right" },
                { key: "status", label: t("status"), render: (r) => <StatusPill status={r.status} /> },
              ]}
            />

            <h3>{t("liveTrades")}</h3>
            <DataTable<LiveTrade>
              rows={state.trades}
              empty={t("empty")}
              columns={[
                { key: "code", label: t("liveCode") },
                { key: "side", label: t("liveSideCol"), render: (r) => t(r.side === "buy" ? "liveBuy" : "liveSell") },
                { key: "price", label: t("livePrice"), align: "right", render: (r) => fmtMoney(r.price) },
                { key: "volume", label: t("liveVolume"), align: "right" },
              ]}
            />
          </div>
        )}
      </section>
    </>
  );
}
