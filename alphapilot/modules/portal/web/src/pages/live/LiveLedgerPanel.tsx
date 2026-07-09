import type { Dispatch, SetStateAction } from "react";
import { Alert, DataTable, InfoDot, RefreshButton } from "../../components";
import { useI18n } from "../../i18n";
import type { AsyncResource, LiveLedgerEvent, LiveLedgerEvents } from "./types";
import { compactJson } from "./utils";

type Props = {
  ledgerEvents: AsyncResource<LiveLedgerEvents>;
  ledgerKind: string;
  setLedgerKind: Dispatch<SetStateAction<string>>;
  ledgerReference: string;
  setLedgerReference: Dispatch<SetStateAction<string>>;
  ledgerLimit: string;
  setLedgerLimit: Dispatch<SetStateAction<string>>;
};

export function LiveLedgerPanel({
  ledgerEvents,
  ledgerKind,
  setLedgerKind,
  ledgerReference,
  setLedgerReference,
  ledgerLimit,
  setLedgerLimit,
}: Props) {
  const { t } = useI18n();
  const ledgerRows = ledgerEvents.data?.events || [];

  return (
    <section className="panel">
      <div className="panel-head">
        <div className="panel-title-inline">
          <h2>{t("liveLedger")}</h2>
          <InfoDot tip={t("liveLedgerTip")} />
        </div>
        <RefreshButton onClick={ledgerEvents.refresh} />
      </div>
      <div className="toolbar live-status-bar">
        <label className="field">
          <span>{t("kind")}</span>
          <input value={ledgerKind} onChange={(e) => setLedgerKind(e.target.value)} placeholder="submit" />
        </label>
        <label className="field">
          <span>{t("liveReference")}</span>
          <input value={ledgerReference} onChange={(e) => setLedgerReference(e.target.value)} />
        </label>
        <label className="field">
          <span>{t("limit")}</span>
          <input value={ledgerLimit} onChange={(e) => setLedgerLimit(e.target.value)} inputMode="numeric" />
        </label>
      </div>
      {ledgerEvents.error ? <Alert tone="error">{ledgerEvents.error}</Alert> : null}
      <DataTable<LiveLedgerEvent>
        rows={ledgerRows}
        loading={ledgerEvents.loading}
        empty={t("empty")}
        columns={[
          { key: "ts", label: t("time"), ellipsis: true },
          { key: "kind", label: t("kind") },
          { key: "source", label: t("source") },
          { key: "order_id", label: t("liveOrderId"), ellipsis: true },
          { key: "reference", label: t("liveReference"), ellipsis: true },
          { key: "payload", label: t("payload"), ellipsis: true, render: (r) => compactJson(r.payload) },
        ]}
      />
    </section>
  );
}
