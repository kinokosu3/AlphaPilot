import { useCallback, useEffect, useMemo, useState } from "react";
import { api, qs } from "../../api";
import { Alert, PageTitle, useConfirm } from "../../components";
import { useAsync, useJsonInput, useSerialPolling } from "../../hooks";
import { useI18n } from "../../i18n";
import { useAction } from "../../toast";
import { LiveActivityTabs } from "./LiveActivityTabs";
import { LiveDiagnosticsDrawer } from "./LiveDiagnosticsDrawer";
import { LiveOrderCard } from "./LiveOrderCard";
import { LiveStatusBar } from "./LiveStatusBar";
import { LiveStrategyCard } from "./LiveStrategyCard";
import type {
  LiveBrokerSpec,
  LiveConnectResult,
  LiveDaemonCommandResult,
  LiveDaemonStatus,
  LiveDaemonStopResult,
  LiveLedgerEvents,
  LiveOrder,
  LivePreflight,
  LivePluginDiagnostics,
  LiveQuoteProviderSpec,
  LiveRiskStatus,
  LiveRuntimeState,
  LiveStatus,
} from "./types";

type Workspace = "live" | "paper";
type SimulationMode = "paper" | "dry_run";
type Ticket = "order" | "target";

const WORKSPACE_STORAGE_KEY = "portal_live_workspace";

function initialWorkspace(): Workspace {
  try {
    return window.localStorage.getItem(WORKSPACE_STORAGE_KEY) === "live" ? "live" : "paper";
  } catch {
    return "paper";
  }
}

const EMPTY_DAEMON: LiveDaemonStatus = { exists: false, path: "", alive: false, running: false, status: "stopped" };
const EMPTY_RUNTIME: LiveRuntimeState = { exists: false, state_path: "" };
const EMPTY_RISK: LiveRiskStatus = { exists: false, state_path: "", ledger_dir: "", recent_rejections: [] };
const EMPTY_LEDGER: LiveLedgerEvents = { count: 0, events: [] };

export function LivePage() {
  const { t } = useI18n();
  const confirm = useConfirm();
  const { run } = useAction();
  const configStatus = useAsync(() => api.get<LiveStatus>("/api/live/status"), []);
  const brokerCatalog = useAsync(() => api.get<LiveBrokerSpec[]>("/api/live/brokers"), []);
  const quoteProviderCatalog = useAsync(() => api.get<LiveQuoteProviderSpec[]>("/api/live/quote-providers"), []);
  const pluginDiagnostics = useAsync(() => api.get<LivePluginDiagnostics>("/api/live/plugins"), []);
  const timingStrategies = useAsync(() => api.get<{ names: string[] }>("/api/timing/strategies"), []);

  const [workspace, setWorkspaceState] = useState<Workspace>(initialWorkspace);
  const [simulationMode, setSimulationMode] = useState<SimulationMode>("paper");
  const [runtimeBroker, setRuntimeBroker] = useState("");
  const [runtimeQuoteProvider, setRuntimeQuoteProvider] = useState("");
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [preflight, setPreflight] = useState<LivePreflight | null>(null);
  const [preflightNetwork, setPreflightNetwork] = useState(false);

  const runtimeMode = workspace === "live" ? "live" : simulationMode;
  const selectedRuntimeBroker = workspace === "live" ? runtimeBroker.trim() : "paper";
  const selectedRuntimeQuoteProvider = workspace === "live" ? (runtimeQuoteProvider.trim() || selectedRuntimeBroker) : "paper";
  const liveBrokerOptions = useMemo(() => brokerCatalog.data || [], [brokerCatalog.data]);
  const quoteProviderOptions = useMemo(() => quoteProviderCatalog.data || [], [quoteProviderCatalog.data]);
  const selectedBrokerSpec = liveBrokerOptions.find((item) => item.name === selectedRuntimeBroker);
  const selectedQuoteProviderSpec = quoteProviderOptions.find((item) => item.name === selectedRuntimeQuoteProvider);
  const catalogsResolved = !brokerCatalog.loading && !quoteProviderCatalog.loading && Boolean(brokerCatalog.data && quoteProviderCatalog.data);
  const providerReady = workspace === "paper" || (catalogsResolved && Boolean(selectedBrokerSpec && selectedQuoteProviderSpec));
  const query = useMemo(() => ({
    mode: runtimeMode,
    broker: selectedRuntimeBroker,
    trade_broker: selectedRuntimeBroker,
    quote_provider: selectedRuntimeQuoteProvider,
  }), [runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider]);

  const daemonStatus = useSerialPolling(
    () => providerReady ? api.get<LiveDaemonStatus>(`/api/live/daemon/status${qs(query)}`) : Promise.resolve(EMPTY_DAEMON),
    [providerReady, runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider],
    { enabled: providerReady, intervalMs: 1000 },
  );
  const runtimeState = useAsync(
    () => providerReady ? api.get<LiveRuntimeState>(`/api/live/runtime/state${qs(query)}`) : Promise.resolve(EMPTY_RUNTIME),
    [providerReady, runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider],
  );
  const riskStatus = useAsync(
    () => providerReady ? api.get<LiveRiskStatus>(`/api/live/risk/status${qs({ ...query, tail: 10 })}`) : Promise.resolve(EMPTY_RISK),
    [providerReady, runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider],
  );
  const [ledgerKind, setLedgerKind] = useState("");
  const [ledgerReference, setLedgerReference] = useState("");
  const [ledgerLimit, setLedgerLimit] = useState("25");
  const ledgerEvents = useAsync(
    () => providerReady ? api.get<LiveLedgerEvents>(`/api/live/ledger/events${qs({
      ...query,
      kind: ledgerKind.trim(),
      reference: ledgerReference.trim(),
      limit: Number(ledgerLimit) || 25,
    })}`) : Promise.resolve(EMPTY_LEDGER),
    [providerReady, runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider, ledgerKind, ledgerReference, ledgerLimit],
  );

  const [daemonSymbols, setDaemonSymbols] = useState("600000");
  const [daemonTimingStrategy, setDaemonTimingStrategy] = useState("");
  const [daemonTimingFreq, setDaemonTimingFreq] = useState("day");
  const daemonTimingParams = useJsonInput("{}");
  const [initialCash, setInitialCash] = useState("1000000");
  const [ticket, setTicket] = useState<Ticket>("order");
  const [orderCode, setOrderCode] = useState("");
  const [orderSide, setOrderSide] = useState("buy");
  const [orderVolume, setOrderVolume] = useState("100");
  const [orderPrice, setOrderPrice] = useState("");
  const [orderType, setOrderType] = useState("limit");
  const [orderExchange, setOrderExchange] = useState("");
  const [orderOffset, setOrderOffset] = useState("none");
  const [orderReference, setOrderReference] = useState("portal_manual");
  const targetJson = useJsonInput('{\n  "holdings": {},\n  "prices": {}\n}');
  const [targetRoute, setTargetRoute] = useState(false);

  const cfg = configStatus.data?.config;

  useEffect(() => {
    if (runtimeBroker.trim() || !catalogsResolved) return;
    const configured = (cfg?.trade_broker || cfg?.broker || "").trim();
    const eligibleConfigured = configured && configured !== "paper" ? liveBrokerOptions.find((item) => item.name === configured) : undefined;
    const fallback = liveBrokerOptions.find((item) => item.gateway_importable) || liveBrokerOptions[0];
    setRuntimeBroker((eligibleConfigured || fallback)?.name || "");
  }, [catalogsResolved, cfg?.broker, cfg?.trade_broker, liveBrokerOptions, runtimeBroker]);

  useEffect(() => {
    if (workspace !== "live" || runtimeQuoteProvider.trim() || !catalogsResolved) return;
    const configured = (cfg?.quote_provider || "").trim();
    const eligibleConfigured = configured && configured !== "paper" ? quoteProviderOptions.find((item) => item.name === configured) : undefined;
    const matching = quoteProviderOptions.find((item) => item.name === runtimeBroker);
    const fallback = quoteProviderOptions.find((item) => item.gateway_importable) || quoteProviderOptions[0];
    setRuntimeQuoteProvider((eligibleConfigured || matching || fallback)?.name || "");
  }, [catalogsResolved, cfg?.quote_provider, quoteProviderOptions, runtimeBroker, runtimeQuoteProvider, workspace]);

  useEffect(() => {
    setPreflight(null);
  }, [runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider, preflightNetwork]);

  useEffect(() => {
    if (!daemonStatus.data?.alive) return;
    const actualMode = daemonStatus.data.mode;
    if (actualMode === "live" && workspace !== "live") {
      setWorkspaceState("live");
      setTargetRoute(false);
    } else if ((actualMode === "paper" || actualMode === "dry_run") && workspace !== "paper") {
      setWorkspaceState("paper");
      setSimulationMode(actualMode);
      setTargetRoute(false);
    }
  }, [daemonStatus.data?.alive, daemonStatus.data?.mode, workspace]);

  const switchWorkspace = (next: Workspace) => {
    if (daemonStatus.data?.alive || next === workspace) return;
    setWorkspaceState(next);
    setTargetRoute(false);
    setOrderCode("");
    targetJson.setRaw('{\n  "holdings": {},\n  "prices": {}\n}');
    try {
      window.localStorage.setItem(WORKSPACE_STORAGE_KEY, next);
    } catch {
      // Storage can be unavailable in privacy-restricted browsers; the safe Paper default remains.
    }
  };

  const closeDiagnostics = useCallback(() => setDiagnosticsOpen(false), []);

  const refreshWorkspace = useCallback(async () => {
    await Promise.all([daemonStatus.refresh(), runtimeState.refresh(), riskStatus.refresh(), ledgerEvents.refresh()]);
  }, [daemonStatus.refresh, runtimeState.refresh, riskStatus.refresh, ledgerEvents.refresh]);

  const checkRuntime = () => run(async () => {
    const result = await api.post<LivePreflight>("/api/live/runtime/preflight", {
      broker: selectedRuntimeBroker,
      trade_broker: selectedRuntimeBroker,
      quote_provider: selectedRuntimeQuoteProvider,
      network: preflightNetwork,
    });
    setPreflight(result);
  }, t("livePreflightDone"));

  const connectRuntime = async () => {
    if (workspace !== "live") return;
    if (!(await confirm({ message: t("liveConnectConfirm"), danger: true }))) return;
    await run(async () => {
      await api.post<LiveConnectResult>("/api/live/runtime/connect", { ...query, timeout: 30 });
      await refreshWorkspace();
    }, t("liveRuntimeConnected"));
  };

  const startDaemon = async () => {
    if (!providerReady) return;
    if (workspace === "live" && (!selectedBrokerSpec?.gateway_importable || !selectedQuoteProviderSpec?.gateway_importable)) return;
    if (workspace === "live" && !(await confirm({ message: t("liveDaemonStartConfirm"), danger: true }))) return;
    await run(async () => {
      const timingParams = daemonTimingStrategy.trim() ? daemonTimingParams.parse() : undefined;
      await api.post("/api/live/daemon/start", {
        ...query,
        cash: workspace === "paper" ? Number(initialCash) || undefined : undefined,
        symbols: daemonSymbols,
        interval: 1,
        record_market_data: true,
        timeout: 30,
        timing_strategy: daemonTimingStrategy.trim() || undefined,
        timing_params: timingParams,
        timing_freq: daemonTimingFreq,
      });
      await refreshWorkspace();
    }, t("liveDaemonStarted"));
  };

  const stopDaemon = async () => {
    if (!(await confirm({ message: t("liveDaemonStopConfirm"), danger: true }))) return;
    await run(async () => {
      const result = await api.post<LiveDaemonStopResult>("/api/live/daemon/stop", { timeout: 5 });
      if (!result.stopped || result.alive || result.running) throw new Error(t("liveDaemonStopFailed"));
      await refreshWorkspace();
    }, t("liveDaemonStopped"));
  };

  const command = (path: string, message: string, payload: Record<string, unknown> = {}) => run(async () => {
    await api.post<LiveDaemonCommandResult>(path, { wait: true, timeout: 5, ...payload });
    await refreshWorkspace();
  }, message);

  const haltDaemon = async () => {
    if (!(await confirm({ message: t("liveHaltConfirm"), danger: true }))) return;
    await command("/api/live/daemon/halt", t("liveHaltedDone"), { reason: "portal" });
  };
  const resumeDaemon = () => command("/api/live/daemon/resume", t("liveResumedDone"));
  const refreshDaemon = () => command("/api/live/daemon/refresh", t("liveDaemonRefreshed"));
  const reconnectDaemon = async () => {
    if (workspace === "live" && !(await confirm({ message: t("liveDaemonReconnectConfirm"), danger: true }))) return;
    await command("/api/live/daemon/reconnect", t("liveDaemonReconnected"), { timeout: 20, auto_resume: false });
  };
  const strategyPause = () => command("/api/live/daemon/strategy/pause", t("liveStrategyPaused"));
  const strategyResume = () => command("/api/live/daemon/strategy/resume", t("liveStrategyResumed"));
  const strategyStop = async () => {
    if (!(await confirm({ message: t("liveStrategyStopConfirm"), danger: true }))) return;
    await command("/api/live/daemon/strategy/stop", t("liveStrategyStopped"));
  };
  const strategyStart = async () => {
    if (workspace === "live" && !(await confirm({ message: t("liveStrategyStartConfirm"), danger: true }))) return;
    await command("/api/live/daemon/strategy/start", t("liveStrategyStarted"), {
      timing_strategy: daemonTimingStrategy.trim(),
      symbols: daemonSymbols,
      timing_params: daemonTimingParams.parse(),
      timing_freq: daemonTimingFreq,
      confirm_live: workspace === "live",
    });
  };

  const submitOrder = async () => {
    if (!orderCode.trim()) return;
    if (workspace === "live" && !(await confirm({ message: t("liveDaemonOrderConfirm"), danger: true }))) return;
    await command("/api/live/daemon/order", t("liveDaemonOrderDone"), {
      symbol: orderCode.trim(),
      side: orderSide,
      volume: Number(orderVolume),
      price: Number(orderPrice) || 0,
      order_type: orderType,
      exchange: orderExchange.trim() || undefined,
      product: "equity",
      offset: orderOffset,
      reference: orderReference.trim() || "portal_manual",
      event_timeout: 3,
      confirm_live: workspace === "live",
    });
  };

  const submitTarget = async () => {
    if (targetRoute && workspace === "live" && !(await confirm({ message: t("liveDaemonTargetConfirm"), danger: true }))) return;
    const parsed = targetJson.parse();
    await command("/api/live/daemon/submit-target", t("liveDaemonTargetDone"), {
      ...parsed,
      source: String(parsed.source || "portal_daemon"),
      route: targetRoute,
      confirm_live: workspace === "live" && targetRoute,
    });
  };

  const cancelOrder = async (order: LiveOrder) => {
    if (!(await confirm({ message: t("liveCancelConfirm"), danger: true }))) return;
    await command("/api/live/daemon/cancel", t("liveCancelRequested"), {
      order_id: order.order_id,
      symbol: `${order.code}.${order.exchange || ""}`.replace(/\.$/, ""),
    });
  };

  const daemon = daemonStatus.data;
  const canStartDaemon = providerReady && (workspace === "paper" || Boolean(selectedBrokerSpec?.gateway_importable && selectedQuoteProviderSpec?.gateway_importable));

  return (
    <div className="stack live-workspace-page">
      <PageTitle title={t("navLive")} subtitle={t("liveWorkspaceIntro")} />
      <div className="live-environment-tabs" role="tablist" aria-label={t("liveEnvironment")}>
        <button type="button" role="tab" aria-selected={workspace === "live"} className={workspace === "live" ? "active live" : ""} disabled={Boolean(daemon?.alive)} onClick={() => switchWorkspace("live")}>{t("liveEnvironmentLive")}</button>
        <button type="button" role="tab" aria-selected={workspace === "paper"} className={workspace === "paper" ? "active" : ""} disabled={Boolean(daemon?.alive)} onClick={() => switchWorkspace("paper")}>{t("liveEnvironmentPaper")}</button>
      </div>
      {workspace === "paper" ? (
        <div className="live-simulation-switch" role="group" aria-label={t("liveSimulationMode")}>
          <button type="button" className={simulationMode === "paper" ? "active" : ""} disabled={Boolean(daemon?.alive)} onClick={() => { setSimulationMode("paper"); setTargetRoute(false); }}>Paper</button>
          <button type="button" className={simulationMode === "dry_run" ? "active" : ""} disabled={Boolean(daemon?.alive)} onClick={() => { setSimulationMode("dry_run"); setTargetRoute(false); }}>Dry-run</button>
        </div>
      ) : null}
      {brokerCatalog.error || quoteProviderCatalog.error ? <Alert tone="error">{brokerCatalog.error || quoteProviderCatalog.error}</Alert> : null}
      <LiveStatusBar workspace={workspace} runtimeMode={runtimeMode} tradeBroker={selectedRuntimeBroker} quoteProvider={selectedRuntimeQuoteProvider} daemon={daemon} onRefresh={refreshWorkspace} onOpenDiagnostics={() => setDiagnosticsOpen(true)} onHalt={haltDaemon} onResume={resumeDaemon} />
      <div className="live-workbench-grid">
        <LiveStrategyCard
          daemon={daemon}
          symbols={daemonSymbols}
          setSymbols={setDaemonSymbols}
          strategy={daemonTimingStrategy}
          setStrategy={setDaemonTimingStrategy}
          strategyNames={timingStrategies.data?.names || []}
          freq={daemonTimingFreq}
          setFreq={setDaemonTimingFreq}
          params={daemonTimingParams}
          initialCash={initialCash}
          setInitialCash={setInitialCash}
          simulated={workspace === "paper"}
          canStartDaemon={canStartDaemon}
          onStartDaemon={startDaemon}
          onStrategyStart={strategyStart}
          onStrategyPause={strategyPause}
          onStrategyResume={strategyResume}
          onStrategyStop={strategyStop}
          onRefreshDaemon={refreshDaemon}
          onReconnectDaemon={reconnectDaemon}
          onStopDaemon={stopDaemon}
        />
        <LiveOrderCard
          daemon={daemon}
          ticket={ticket}
          setTicket={setTicket}
          code={orderCode}
          setCode={setOrderCode}
          side={orderSide}
          setSide={setOrderSide}
          volume={orderVolume}
          setVolume={setOrderVolume}
          price={orderPrice}
          setPrice={setOrderPrice}
          orderType={orderType}
          setOrderType={setOrderType}
          exchange={orderExchange}
          setExchange={setOrderExchange}
          offset={orderOffset}
          setOffset={setOrderOffset}
          reference={orderReference}
          setReference={setOrderReference}
          target={targetJson}
          routeTarget={targetRoute}
          setRouteTarget={setTargetRoute}
          workspace={workspace}
          onSubmitOrder={submitOrder}
          onSubmitTarget={submitTarget}
        />
      </div>
      <LiveActivityTabs
        daemon={daemon}
        mode={runtimeMode}
        tradeBroker={selectedRuntimeBroker}
        quoteProvider={selectedRuntimeQuoteProvider}
        ledger={ledgerEvents}
        ledgerKind={ledgerKind}
        setLedgerKind={setLedgerKind}
        ledgerReference={ledgerReference}
        setLedgerReference={setLedgerReference}
        ledgerLimit={ledgerLimit}
        setLedgerLimit={setLedgerLimit}
        onCancelOrder={cancelOrder}
      />
      <LiveDiagnosticsDrawer
        open={diagnosticsOpen}
        onClose={closeDiagnostics}
        workspace={workspace}
        runtimeBroker={runtimeBroker}
        setRuntimeBroker={setRuntimeBroker}
        runtimeQuoteProvider={runtimeQuoteProvider}
        setRuntimeQuoteProvider={setRuntimeQuoteProvider}
        brokers={liveBrokerOptions}
        quoteProviders={quoteProviderOptions}
        providerSelectionLocked={Boolean(daemon?.alive)}
        providerReady={providerReady}
        cfg={cfg}
        daemon={daemon}
        runtimeState={runtimeState}
        riskStatus={riskStatus}
        pluginDiagnostics={pluginDiagnostics}
        preflight={preflight}
        preflightNetwork={preflightNetwork}
        setPreflightNetwork={setPreflightNetwork}
        onPreflight={checkRuntime}
        onConnect={connectRuntime}
      />
    </div>
  );
}
