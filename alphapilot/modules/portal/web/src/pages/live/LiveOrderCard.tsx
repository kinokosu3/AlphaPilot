import type { Dispatch, SetStateAction } from "react";
import { Alert, AsyncButton } from "../../components";
import { useI18n } from "../../i18n";
import type { JsonInputState, LiveDaemonStatus } from "./types";

type Ticket = "order" | "target";

type Props = {
  daemon?: LiveDaemonStatus | null;
  ticket: Ticket;
  setTicket: Dispatch<SetStateAction<Ticket>>;
  code: string;
  setCode: Dispatch<SetStateAction<string>>;
  side: string;
  setSide: Dispatch<SetStateAction<string>>;
  volume: string;
  setVolume: Dispatch<SetStateAction<string>>;
  price: string;
  setPrice: Dispatch<SetStateAction<string>>;
  orderType: string;
  setOrderType: Dispatch<SetStateAction<string>>;
  exchange: string;
  setExchange: Dispatch<SetStateAction<string>>;
  offset: string;
  setOffset: Dispatch<SetStateAction<string>>;
  reference: string;
  setReference: Dispatch<SetStateAction<string>>;
  target: JsonInputState;
  routeTarget: boolean;
  setRouteTarget: Dispatch<SetStateAction<boolean>>;
  workspace: "live" | "shadow" | "simulation" | "paper";
  routeDisabled?: boolean;
  onSubmitOrder: () => void | Promise<unknown>;
  onSubmitTarget: () => void | Promise<unknown>;
};

export function LiveOrderCard(props: Props) {
  const { t } = useI18n();
  const disabled = !props.daemon?.running || Boolean(props.routeDisabled);

  return (
    <section className="panel live-work-card" aria-labelledby="live-order-title">
      <div className="panel-head">
        <div>
          <h2 id="live-order-title">{t("liveManualWorkspace")}</h2>
          <span className="muted">{t("liveManualWorkspaceHint")}</span>
        </div>
        {props.workspace === "live" ? <span className="live-environment-badge live">{t("liveEnvironmentLive")}</span> : null}
        {props.workspace === "shadow" ? <span className="live-environment-badge shadow">SHADOW</span> : null}
        {props.workspace === "simulation" ? <span className="live-environment-badge simulation">{t("liveEnvironmentSimulation")}</span> : null}
      </div>
      {!props.daemon?.running ? <Alert tone="info">{t("liveManualDaemonRequired")}</Alert> : null}
      <div className="live-subtabs" role="tablist" aria-label={t("liveManualWorkspace")}>
        <button type="button" role="tab" aria-selected={props.ticket === "order"} className={props.ticket === "order" ? "active" : ""} onClick={() => props.setTicket("order")}>{t("liveNormalOrder")}</button>
        <button type="button" role="tab" aria-selected={props.ticket === "target"} className={props.ticket === "target" ? "active" : ""} onClick={() => props.setTicket("target")}>{t("liveTargetPosition")}</button>
      </div>
      {props.ticket === "order" ? (
        <div role="tabpanel" className="stack compact">
          <div className="live-form-grid">
            <label className="field live-field-wide"><span>{t("liveCode")}</span><input value={props.code} onChange={(event) => props.setCode(event.target.value)} placeholder={props.workspace === "live" ? t("liveNoDefaultSymbol") : "SH600000"} /></label>
            <label className="field"><span>{t("liveSideCol")}</span><select value={props.side} onChange={(event) => props.setSide(event.target.value)}><option value="buy">{t("liveBuy")}</option><option value="sell">{t("liveSell")}</option></select></label>
            <label className="field"><span>{t("livePrice")}</span><input value={props.price} onChange={(event) => props.setPrice(event.target.value)} inputMode="decimal" /></label>
            <label className="field"><span>{t("liveVolume")}</span><input value={props.volume} onChange={(event) => props.setVolume(event.target.value)} inputMode="numeric" /></label>
            <label className="field"><span>{t("liveOrderType")}</span><select value={props.orderType} onChange={(event) => props.setOrderType(event.target.value)}><option value="limit">limit</option><option value="market">market</option><option value="fak">FAK</option><option value="fok">FOK</option></select></label>
          </div>
          <details className="live-advanced">
            <summary>{t("liveAdvancedOrder")}</summary>
            <div className="live-form-grid">
              <label className="field"><span>{t("liveExchange")}</span><input value={props.exchange} onChange={(event) => props.setExchange(event.target.value)} /></label>
              <label className="field"><span>{t("liveOffset")}</span><select value={props.offset} onChange={(event) => props.setOffset(event.target.value)}><option value="none">none</option><option value="open">open</option><option value="close">close</option><option value="close_today">close_today</option></select></label>
              <label className="field live-field-wide"><span>{t("liveReference")}</span><input value={props.reference} onChange={(event) => props.setReference(event.target.value)} /></label>
            </div>
          </details>
          <AsyncButton onClick={props.onSubmitOrder} disabled={disabled || !props.code.trim()}>{t("liveSubmitOrder")}</AsyncButton>
        </div>
      ) : (
        <div role="tabpanel" className="stack compact">
          <label className="field"><span>{t("liveTargetPosition")}</span><textarea rows={8} value={props.target.raw} onChange={(event) => props.target.setRaw(event.target.value)} spellCheck={false} /></label>
          <label className="inline-check compact">
            <input aria-label={t("liveRouteTarget")} type="checkbox" checked={props.routeTarget} disabled={props.routeDisabled} onChange={(event) => props.setRouteTarget(event.target.checked)} />
            <span>{props.routeTarget ? t("liveRouteTarget") : t("livePlanTarget")}</span>
          </label>
          <p className="muted compact">{t("liveDaemonTargetHint")}</p>
          <AsyncButton onClick={props.onSubmitTarget} disabled={disabled}>{t("liveSubmit")}</AsyncButton>
        </div>
      )}
    </section>
  );
}
