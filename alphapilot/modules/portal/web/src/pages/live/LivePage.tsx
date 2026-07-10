import { useEffect, useMemo, useState } from "react";
import { api, qs } from "../../api";
import { Alert, PageTitle, useConfirm } from "../../components";
import { useAsync, useJsonInput } from "../../hooks";
import { useI18n } from "../../i18n";
import { useAction } from "../../toast";
import { LiveDaemonPanel } from "./LiveDaemonPanel";
import { LiveLedgerPanel } from "./LiveLedgerPanel";
import { LivePaperSandbox } from "./LivePaperSandbox";
import { LiveRuntimePanel } from "./LiveRuntimePanel";
import type {
  LiveBrokerSpec,
  LiveConnectResult,
  LiveDaemonCommandResult,
  LiveDaemonStatus,
  LiveLedgerEvents,
  LiveOrder,
  LivePreflight,
  LivePluginDiagnostics,
  LiveQuoteProviderSpec,
  LiveRiskStatus,
  LiveRuntimeState,
  LiveStatus,
} from "./types";
import { fmtMoney } from "./utils";

export function LivePage() {
  const { t } = useI18n();
  const confirm = useConfirm();
  const status = useAsync(() => api.get<LiveStatus>("/api/live/status"), []);
  const brokerCatalog = useAsync(() => api.get<LiveBrokerSpec[]>("/api/live/brokers"), []);
  const quoteProviderCatalog = useAsync(() => api.get<LiveQuoteProviderSpec[]>("/api/live/quote-providers"), []);
  const pluginDiagnostics = useAsync(() => api.get<LivePluginDiagnostics>("/api/live/plugins"), []);
  const { run } = useAction();
  const [runtimeMode, setRuntimeMode] = useState("live");
  const [runtimeBroker, setRuntimeBroker] = useState("");
  const [runtimeQuoteProvider, setRuntimeQuoteProvider] = useState("");
  const selectedRuntimeBroker = runtimeMode === "live" ? runtimeBroker.trim() : "paper";
  const selectedRuntimeQuoteProvider = runtimeMode === "live" ? (runtimeQuoteProvider.trim() || selectedRuntimeBroker) : "paper";
  const runtimeState = useAsync(
    () => api.get<LiveRuntimeState>(`/api/live/runtime/state${qs({
      mode: runtimeMode,
      broker: selectedRuntimeBroker,
      trade_broker: selectedRuntimeBroker,
      quote_provider: selectedRuntimeQuoteProvider,
    })}`),
    [runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider],
  );
  const daemonStatus = useAsync(
    () => api.get<LiveDaemonStatus>(`/api/live/daemon/status${qs({
      mode: runtimeMode,
      broker: selectedRuntimeBroker,
      trade_broker: selectedRuntimeBroker,
      quote_provider: selectedRuntimeQuoteProvider,
    })}`),
    [runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider],
  );
  const [preflight, setPreflight] = useState<LivePreflight | null>(null);
  const [preflightNetwork, setPreflightNetwork] = useState(false);
  const [ledgerKind, setLedgerKind] = useState("");
  const [ledgerReference, setLedgerReference] = useState("");
  const [ledgerLimit, setLedgerLimit] = useState("25");
  const riskStatus = useAsync(
    () => api.get<LiveRiskStatus>(`/api/live/risk/status${qs({
      mode: runtimeMode,
      broker: selectedRuntimeBroker,
      trade_broker: selectedRuntimeBroker,
      quote_provider: selectedRuntimeQuoteProvider,
      tail: 10,
    })}`),
    [runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider],
  );
  const ledgerEvents = useAsync(
    () => api.get<LiveLedgerEvents>(`/api/live/ledger/events${qs({
      mode: runtimeMode,
      broker: selectedRuntimeBroker,
      trade_broker: selectedRuntimeBroker,
      quote_provider: selectedRuntimeQuoteProvider,
      kind: ledgerKind.trim(),
      reference: ledgerReference.trim(),
      limit: Number(ledgerLimit) || 25,
    })}`),
    [runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider, ledgerKind, ledgerReference, ledgerLimit],
  );
  const [daemonSymbols, setDaemonSymbols] = useState("600000");
  const [daemonTimingStrategy, setDaemonTimingStrategy] = useState("");
  const [daemonTimingFreq, setDaemonTimingFreq] = useState("day");
  const daemonTimingParams = useJsonInput("{}");
  const [daemonOrderCode, setDaemonOrderCode] = useState("SH600000");
  const [daemonOrderSide, setDaemonOrderSide] = useState("buy");
  const [daemonOrderVol, setDaemonOrderVol] = useState("100");
  const [daemonOrderPrice, setDaemonOrderPrice] = useState("");
  const [daemonOrderType, setDaemonOrderType] = useState("limit");
  const [daemonOrderExchange, setDaemonOrderExchange] = useState("");
  const [daemonOrderProduct, setDaemonOrderProduct] = useState("equity");
  const [daemonOrderOffset, setDaemonOrderOffset] = useState("none");
  const [daemonOrderRef, setDaemonOrderRef] = useState("portal_manual");
  const daemonTargetJson = useJsonInput(
    '{\n  "holdings": { "SH600000": 1000 },\n  "prices": { "SH600000": 10.0 }\n}',
  );
  const [daemonTargetRoute, setDaemonTargetRoute] = useState(false);
  const [cash, setCash] = useState("1000000");
  const [orderCode, setOrderCode] = useState("");
  const [orderSide, setOrderSide] = useState("buy");
  const [orderVol, setOrderVol] = useState("100");
  const [orderPrice, setOrderPrice] = useState("");
  const [targetJson, setTargetJson] = useState(
    '{\n  "holdings": { "SH600000": 1000 },\n  "prices": { "SH600000": 10.0 }\n}',
  );

  const cfg = status.data?.config;
  const liveBrokerOptions = useMemo(() => brokerCatalog.data || [], [brokerCatalog.data]);
  const quoteProviderOptions = useMemo(() => quoteProviderCatalog.data || [], [quoteProviderCatalog.data]);
  const selectedBrokerSpec = liveBrokerOptions.find((item) => item.name === selectedRuntimeBroker);
  const selectedQuoteProviderSpec = quoteProviderOptions.find((item) => item.name === selectedRuntimeQuoteProvider);
  const running = Boolean(status.data?.running);
  const state = status.data?.state;

  useEffect(() => {
    if (runtimeBroker.trim()) return;
    const configured = (cfg?.trade_broker || cfg?.broker) && (cfg?.trade_broker || cfg?.broker) !== "paper" ? (cfg?.trade_broker || cfg?.broker || "") : "";
    const configuredBroker = configured ? liveBrokerOptions.find((item) => item.name === configured) : undefined;
    if (configured && !configuredBroker) {
      setRuntimeBroker(configured);
      return;
    }
    if (!liveBrokerOptions.length) return;
    const importableBroker = liveBrokerOptions.find((item) => item.gateway_importable);
    setRuntimeBroker((configuredBroker || importableBroker || liveBrokerOptions[0]).name);
  }, [cfg?.broker, cfg?.trade_broker, liveBrokerOptions, runtimeBroker]);

  useEffect(() => {
    if (runtimeMode !== "live") {
      setRuntimeQuoteProvider("");
      return;
    }
    if (runtimeQuoteProvider.trim()) return;
    const configured = cfg?.quote_provider && cfg.quote_provider !== "paper" ? cfg.quote_provider : "";
    const configuredProvider = configured ? quoteProviderOptions.find((item) => item.name === configured) : undefined;
    if (configured && !configuredProvider) {
      setRuntimeQuoteProvider(configured);
      return;
    }
    if (!quoteProviderOptions.length) return;
    const matchingTrade = selectedRuntimeBroker ? quoteProviderOptions.find((item) => item.name === selectedRuntimeBroker) : undefined;
    const importableProvider = quoteProviderOptions.find((item) => item.gateway_importable);
    setRuntimeQuoteProvider((configuredProvider || matchingTrade || importableProvider || quoteProviderOptions[0]).name);
  }, [cfg?.quote_provider, quoteProviderOptions, runtimeMode, runtimeQuoteProvider, selectedRuntimeBroker]);

  useEffect(() => {
    setPreflight(null);
  }, [runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider, preflightNetwork]);

  const refreshLiveOps = async () => {
    await Promise.all([runtimeState.refresh(), daemonStatus.refresh(), riskStatus.refresh(), ledgerEvents.refresh()]);
  };

  const checkRuntime = () =>
    run(async () => {
      const result = await api.post<LivePreflight>("/api/live/runtime/preflight", {
        broker: selectedRuntimeBroker || undefined,
        trade_broker: selectedRuntimeBroker || undefined,
        quote_provider: selectedRuntimeQuoteProvider || undefined,
        network: preflightNetwork,
      });
      setPreflight(result);
    }, t("livePreflightDone"));

  const connectRuntime = async () => {
    if (runtimeMode === "live" && !(await confirm({ message: t("liveConnectConfirm"), danger: true }))) return;
    await run(async () => {
      await api.post<LiveConnectResult>("/api/live/runtime/connect", {
        mode: runtimeMode,
        broker: selectedRuntimeBroker || undefined,
        trade_broker: selectedRuntimeBroker || undefined,
        quote_provider: selectedRuntimeQuoteProvider || undefined,
        timeout: 30,
      });
      await refreshLiveOps();
    }, t("liveRuntimeConnected"));
  };

  const startDaemon = async () => {
    if (runtimeMode === "live" && (!selectedBrokerSpec?.gateway_importable || !selectedQuoteProviderSpec?.gateway_importable)) return;
    if (runtimeMode === "live" && !(await confirm({ message: t("liveDaemonStartConfirm"), danger: true }))) return;
    await run(async () => {
      const timingParams = daemonTimingStrategy.trim() ? daemonTimingParams.parse() : undefined;
      await api.post("/api/live/daemon/start", {
        mode: runtimeMode,
        broker: selectedRuntimeBroker || undefined,
        trade_broker: selectedRuntimeBroker || undefined,
        quote_provider: selectedRuntimeQuoteProvider || undefined,
        symbols: daemonSymbols,
        interval: 2,
        timeout: 30,
        timing_strategy: daemonTimingStrategy.trim() || undefined,
        timing_params: timingParams,
        timing_freq: daemonTimingFreq,
      });
      await refreshLiveOps();
    }, t("liveDaemonStarted"));
  };

  const stopDaemon = async () => {
    if (!(await confirm({ message: t("liveDaemonStopConfirm"), danger: true }))) return;
    await run(async () => {
      await api.post("/api/live/daemon/stop", { timeout: 5 });
      await refreshLiveOps();
    }, t("liveDaemonStopped"));
  };

  const haltDaemon = async () => {
    if (!(await confirm({ message: t("liveHaltConfirm"), danger: true }))) return;
    await run(async () => {
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/halt", { reason: "portal", wait: true, timeout: 5 });
      await refreshLiveOps();
    }, t("liveHaltedDone"));
  };

  const resumeDaemon = () =>
    run(async () => {
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/resume", { wait: true, timeout: 5 });
      await refreshLiveOps();
    }, t("liveResumedDone"));

  const refreshDaemon = () =>
    run(async () => {
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/refresh", { wait: true, timeout: 5 });
      await refreshLiveOps();
    }, t("liveDaemonRefreshed"));

  const reconnectDaemon = async () => {
    if (runtimeMode === "live" && !(await confirm({ message: t("liveDaemonReconnectConfirm"), danger: true }))) return;
    await run(async () => {
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/reconnect", { wait: true, timeout: 20, auto_resume: false });
      await refreshLiveOps();
    }, t("liveDaemonReconnected"));
  };

  const cancelDaemonOrder = async (order: LiveOrder) => {
    if (!(await confirm({ message: t("liveCancelConfirm"), danger: true }))) return;
    await run(async () => {
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/cancel", {
        order_id: order.order_id,
        symbol: `${order.code}.${order.exchange || ""}`.replace(/\.$/, ""),
        wait: true,
        timeout: 5,
      });
      await refreshLiveOps();
    }, t("liveCancelRequested"));
  };

  const strategyStart = async () => {
    if (runtimeMode === "live" && !(await confirm({ message: t("liveStrategyStartConfirm"), danger: true }))) return;
    await run(async () => {
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/strategy/start", {
        timing_strategy: daemonTimingStrategy.trim(),
        symbols: daemonSymbols,
        timing_params: daemonTimingParams.parse(),
        timing_freq: daemonTimingFreq,
        wait: true,
        timeout: 5,
        confirm_live: runtimeMode === "live",
      });
      await refreshLiveOps();
    }, t("liveStrategyStarted"));
  };

  const strategyStatus = () =>
    run(async () => {
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/strategy/status", { wait: true, timeout: 5 });
      await refreshLiveOps();
    }, t("liveStrategyStatusDone"));

  const strategyPause = () =>
    run(async () => {
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/strategy/pause", { wait: true, timeout: 5 });
      await refreshLiveOps();
    }, t("liveStrategyPaused"));

  const strategyResume = () =>
    run(async () => {
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/strategy/resume", { wait: true, timeout: 5 });
      await refreshLiveOps();
    }, t("liveStrategyResumed"));

  const strategyStop = async () => {
    if (!(await confirm({ message: t("liveStrategyStopConfirm"), danger: true }))) return;
    await run(async () => {
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/strategy/stop", { wait: true, timeout: 5 });
      await refreshLiveOps();
    }, t("liveStrategyStopped"));
  };

  const submitDaemonOrder = async () => {
    if (daemonOrderProduct === "futures") return;
    if (runtimeMode === "live" && !(await confirm({ message: t("liveDaemonOrderConfirm"), danger: true }))) return;
    await run(async () => {
      if (!daemonOrderCode.trim()) throw new Error(t("liveCode"));
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/order", {
        symbol: daemonOrderCode.trim(),
        side: daemonOrderSide,
        volume: Number(daemonOrderVol),
        price: Number(daemonOrderPrice) || 0,
        order_type: daemonOrderType,
        exchange: daemonOrderExchange.trim() || undefined,
        product: daemonOrderProduct,
        offset: daemonOrderOffset,
        reference: daemonOrderRef.trim() || "portal_manual",
        wait: true,
        timeout: 5,
        event_timeout: 3,
        confirm_live: runtimeMode === "live",
      });
      await refreshLiveOps();
    }, t("liveDaemonOrderDone"));
  };

  const submitDaemonTarget = async () => {
    if (daemonTargetRoute && runtimeMode === "live" && !(await confirm({ message: t("liveDaemonTargetConfirm"), danger: true }))) return;
    await run(async () => {
      const parsed = daemonTargetJson.parse();
      await api.post<LiveDaemonCommandResult>("/api/live/daemon/submit-target", {
        ...parsed,
        source: String(parsed.source || "portal_daemon"),
        route: daemonTargetRoute,
        wait: true,
        timeout: 5,
        confirm_live: runtimeMode === "live" && daemonTargetRoute,
      });
      await refreshLiveOps();
    }, t("liveDaemonTargetDone"));
  };

  const connect = () =>
    run(async () => {
      await api.post("/api/live/paper/connect", { cash: Number(cash) || undefined });
      await status.refresh();
    }, t("liveConnected"));

  const submitOrder = () =>
    run(async () => {
      if (!orderCode.trim()) throw new Error(t("liveCode"));
      await api.post("/api/live/paper/order", {
        code: orderCode.trim(),
        side: orderSide,
        volume: Number(orderVol),
        price: Number(orderPrice) || undefined,
      });
      await status.refresh();
    }, t("liveOrderDone"));

  const submitTarget = () =>
    run(async () => {
      await api.post("/api/live/paper/submit-target", JSON.parse(targetJson));
      await status.refresh();
    }, t("liveTargetDone"));

  const halt = async () => {
    if (!(await confirm({ message: t("liveHaltConfirm"), danger: true }))) return;
    await run(async () => {
      await api.post("/api/live/paper/halt", {});
      await status.refresh();
    }, t("liveHaltedDone"));
  };

  const resume = () =>
    run(async () => {
      await api.post("/api/live/paper/resume", {});
      await status.refresh();
    }, t("liveResumedDone"));

  const reset = async () => {
    if (!(await confirm({ message: t("liveResetConfirm"), danger: true }))) return;
    await run(async () => {
      await api.post("/api/live/paper/reset", {});
      await status.refresh();
    }, t("liveResetDone"));
  };

  const riskRows: Array<[string, string]> = cfg
    ? [
        [t("liveRiskMaxOrder"), fmtMoney(cfg.risk.max_order_value)],
        [t("liveRiskMaxDaily"), fmtMoney(cfg.risk.max_daily_value)],
        [t("liveRiskMaxPos"), `${Math.round((cfg.risk.max_position_pct || 0) * 100)}%`],
        [t("liveRiskPriceGuard"), `${Math.round((cfg.risk.price_guard_pct || 0) * 100)}%`],
        [t("liveRiskLot"), String(cfg.risk.lot_size)],
        [t("liveRiskMaxOrders"), String(cfg.risk.max_orders_per_day)],
      ]
    : [];

  return (
    <div className="stack">
      <PageTitle title={t("navLive")} subtitle={t("liveIntro")} />
      <Alert tone="info">{t("liveRuntimeNote")}</Alert>

      <LiveRuntimePanel
        cfg={cfg}
        riskRows={riskRows}
        onRefreshStatus={status.refresh}
        runtimeMode={runtimeMode}
        setRuntimeMode={setRuntimeMode}
        runtimeBroker={runtimeBroker}
        setRuntimeBroker={setRuntimeBroker}
        selectedRuntimeBroker={selectedRuntimeBroker}
        liveBrokerOptions={liveBrokerOptions}
        selectedBrokerSpec={selectedBrokerSpec}
        quoteProviderOptions={quoteProviderOptions}
        selectedRuntimeQuoteProvider={selectedRuntimeQuoteProvider}
        runtimeQuoteProvider={runtimeQuoteProvider}
        setRuntimeQuoteProvider={setRuntimeQuoteProvider}
        selectedQuoteProviderSpec={selectedQuoteProviderSpec}
        quoteProviderCatalog={quoteProviderCatalog}
        brokerCatalog={brokerCatalog}
        preflight={preflight}
        preflightNetwork={preflightNetwork}
        setPreflightNetwork={setPreflightNetwork}
        onCheckRuntime={checkRuntime}
        onConnectRuntime={connectRuntime}
        runtimeState={runtimeState}
        riskStatus={riskStatus}
        pluginDiagnostics={pluginDiagnostics}
        providerSelectionLocked={Boolean(daemonStatus.data?.alive)}
      />

      <LiveDaemonPanel
        daemonStatus={daemonStatus}
        daemonSymbols={daemonSymbols}
        setDaemonSymbols={setDaemonSymbols}
        daemonTimingStrategy={daemonTimingStrategy}
        setDaemonTimingStrategy={setDaemonTimingStrategy}
        daemonTimingFreq={daemonTimingFreq}
        setDaemonTimingFreq={setDaemonTimingFreq}
        daemonTimingParams={daemonTimingParams}
        daemonOrderCode={daemonOrderCode}
        setDaemonOrderCode={setDaemonOrderCode}
        daemonOrderSide={daemonOrderSide}
        setDaemonOrderSide={setDaemonOrderSide}
        daemonOrderVol={daemonOrderVol}
        setDaemonOrderVol={setDaemonOrderVol}
        daemonOrderPrice={daemonOrderPrice}
        setDaemonOrderPrice={setDaemonOrderPrice}
        daemonOrderType={daemonOrderType}
        setDaemonOrderType={setDaemonOrderType}
        daemonOrderExchange={daemonOrderExchange}
        setDaemonOrderExchange={setDaemonOrderExchange}
        daemonOrderProduct={daemonOrderProduct}
        setDaemonOrderProduct={setDaemonOrderProduct}
        daemonOrderOffset={daemonOrderOffset}
        setDaemonOrderOffset={setDaemonOrderOffset}
        daemonOrderRef={daemonOrderRef}
        setDaemonOrderRef={setDaemonOrderRef}
        daemonTargetJson={daemonTargetJson}
        daemonTargetRoute={daemonTargetRoute}
        setDaemonTargetRoute={setDaemonTargetRoute}
        onStrategyStatus={strategyStatus}
        onStrategyStart={strategyStart}
        onStrategyPause={strategyPause}
        onStrategyResume={strategyResume}
        onStrategyStop={strategyStop}
        onStartDaemon={startDaemon}
        canStartDaemon={runtimeMode !== "live" || Boolean(selectedBrokerSpec?.gateway_importable && selectedQuoteProviderSpec?.gateway_importable)}
        onStopDaemon={stopDaemon}
        onHaltDaemon={haltDaemon}
        onResumeDaemon={resumeDaemon}
        onRefreshDaemon={refreshDaemon}
        onReconnectDaemon={reconnectDaemon}
        onCancelDaemonOrder={cancelDaemonOrder}
        onSubmitDaemonOrder={submitDaemonOrder}
        onSubmitDaemonTarget={submitDaemonTarget}
      />

      <LiveLedgerPanel
        ledgerEvents={ledgerEvents}
        ledgerKind={ledgerKind}
        setLedgerKind={setLedgerKind}
        ledgerReference={ledgerReference}
        setLedgerReference={setLedgerReference}
        ledgerLimit={ledgerLimit}
        setLedgerLimit={setLedgerLimit}
      />

      <LivePaperSandbox
        running={running}
        state={state}
        cash={cash}
        setCash={setCash}
        orderCode={orderCode}
        setOrderCode={setOrderCode}
        orderSide={orderSide}
        setOrderSide={setOrderSide}
        orderVol={orderVol}
        setOrderVol={setOrderVol}
        orderPrice={orderPrice}
        setOrderPrice={setOrderPrice}
        targetJson={targetJson}
        setTargetJson={setTargetJson}
        onConnect={connect}
        onSubmitOrder={submitOrder}
        onSubmitTarget={submitTarget}
        onHalt={halt}
        onResume={resume}
        onReset={reset}
      />
    </div>
  );
}
