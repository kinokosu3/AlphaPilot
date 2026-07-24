import { useCallback, useEffect, useMemo, useState } from "react";
import { api, getOperatorToken, qs, setOperatorToken } from "../../api";
import { Alert, PageTitle, useConfirm } from "../../components";
import { useAsync, useJsonInput, useSerialPolling } from "../../hooks";
import { useI18n } from "../../i18n";
import { useAction } from "../../toast";
import { LiveActivityTabs } from "./LiveActivityTabs";
import { LiveDiagnosticsDrawer } from "./LiveDiagnosticsDrawer";
import { LiveOrderCard } from "./LiveOrderCard";
import { LiveProviderCard } from "./LiveProviderCard";
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

type Workspace = "live" | "shadow" | "simulation" | "paper";
type SimulationMode = "paper" | "dry_run";
type Ticket = "order" | "target";

type DeploymentPackage = {
  instance?: { instance_id?: string; validation_state?: string; config_hash?: string };
  configuration?: DeploymentSpec;
  runtime?: {
    desired_state?: string; observed_state?: string; account_id_hash?: string; run_mode?: string;
    runtime_id?: string; runner_heartbeat_at?: string; reconcile_required?: boolean; last_error?: Record<string, unknown>;
    execution_environment?: string; trade_provider?: string; quote_provider?: string;
    quote_data_kind?: string; binding_hash?: string; reconciled?: boolean;
  };
  runs?: Array<{
    run_id: string; run_mode: string; status: string; trading_sessions: number;
    config_hash: string; metrics?: Record<string, unknown>;
  }>;
  route_blocks?: Array<{ scope_type: string; scope_id: string; active: boolean; reason?: string }>;
};

type DeploymentSpec = {
  instance_id: string; execution_environment: "local_paper" | "broker_simulation" | "live";
  config_hash: string; run_mode: Workspace;
  trade_provider: string; quote_provider: string; account_profile: string;
  account_id_hash?: string;
  quote_data_kind: "realtime" | "replay" | "synthetic"; binding_hash: string; version: number;
  stale?: boolean;
};

type RuntimeDiagnostics = {
  config_hash?: string;
  modes?: Record<string, {
    run_count?: number; completed_runs?: number; trading_sessions?: number;
    failures?: Record<string, number>;
  }>;
};

type BrokerUATRun = {
  run_id: string; broker: string; status: string; symbol: string; current_step?: string;
  environment?: string; plugin_version?: string; sdk_version?: string; created_at?: string; ended_at?: string;
  evidence?: { expires_at?: string; evidence_hash?: string } | null;
};

const WORKSPACE_STORAGE_KEY = "portal_live_workspace";

function initialWorkspace(): Workspace {
  try {
    const stored = window.localStorage.getItem(WORKSPACE_STORAGE_KEY);
    return stored === "live" || stored === "shadow" || stored === "simulation" ? stored : "paper";
  } catch {
    return "paper";
  }
}

function rememberWorkspace(workspace: Workspace): void {
  try {
    window.localStorage.setItem(WORKSPACE_STORAGE_KEY, workspace);
  } catch {
    // Storage can be unavailable in privacy-restricted browsers; Paper remains the safe default.
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
  const strategyInstances = useAsync(
    () => api.get<{ instances: Array<{ instance_id: string; validation_state: string; config?: { frequency?: string; universe?: string[] } }> }>("/api/trading/strategy-instances"),
    [],
  );

  const [workspace, setWorkspaceState] = useState<Workspace>(initialWorkspace);
  const [simulationMode, setSimulationMode] = useState<SimulationMode>("paper");
  const [runtimeBroker, setRuntimeBroker] = useState("");
  const [runtimeQuoteProvider, setRuntimeQuoteProvider] = useState("");
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [preflight, setPreflight] = useState<LivePreflight | null>(null);
  const [preflightNetwork, setPreflightNetwork] = useState(false);

  const runtimeMode = workspace === "paper" ? simulationMode : workspace;
  const selectedRuntimeBroker = workspace === "paper" ? "paper" : runtimeBroker.trim();
  const selectedRuntimeQuoteProvider = workspace === "paper" ? "paper" : (runtimeQuoteProvider.trim() || selectedRuntimeBroker);
  const liveBrokerOptions = useMemo(
    () => (brokerCatalog.data || []).filter((item) => (
      workspace === "simulation"
        ? item.account_kind === "simulation"
        : (item.account_kind || "live") === "live"
    )),
    [brokerCatalog.data, workspace],
  );
  const quoteProviderOptions = useMemo(
    () => (quoteProviderCatalog.data || []).filter((item) => (
      workspace === "live" || workspace === "shadow" ? (item.data_kind || "realtime") === "realtime"
      : workspace === "simulation" ? ["realtime", "replay"].includes(item.data_kind || "")
      : item.name === "paper"
    )),
    [quoteProviderCatalog.data, workspace],
  );
  const selectedBrokerSpec = liveBrokerOptions.find((item) => item.name === selectedRuntimeBroker);
  const selectedQuoteProviderSpec = quoteProviderOptions.find((item) => item.name === selectedRuntimeQuoteProvider);
  const catalogsResolved = !brokerCatalog.loading && !quoteProviderCatalog.loading && Boolean(brokerCatalog.data && quoteProviderCatalog.data);
  const providerReady = workspace === "paper" || (catalogsResolved && Boolean(selectedBrokerSpec && selectedQuoteProviderSpec));
  const observeOnlyQuotes = workspace === "shadow" || (
    workspace === "simulation" && selectedQuoteProviderSpec?.data_kind !== "realtime"
  );
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
  const [operatorToken, setOperatorTokenValue] = useState(getOperatorToken());
  const [killReason, setKillReason] = useState("");
  const deploymentEvidence = useAsync(
    () => daemonTimingStrategy.trim()
      ? api.get<DeploymentPackage>(`/api/trading/deployments/${daemonTimingStrategy.trim()}`)
      : Promise.resolve({} as DeploymentPackage),
    [daemonTimingStrategy],
  );
  const [accountProfile, setAccountProfile] = useState("");
  const [deploymentAccountId, setDeploymentAccountId] = useState("");
  const [deploymentReason, setDeploymentReason] = useState("");
  const deploymentDiagnostics = useAsync(
    () => daemonTimingStrategy.trim()
      ? api.get<RuntimeDiagnostics>(`/api/trading/deployments/${daemonTimingStrategy.trim()}/diagnostics`)
      : Promise.resolve({} as RuntimeDiagnostics),
    [daemonTimingStrategy],
  );
  const brokerUatRuns = useAsync(
    () => api.get<{ runs: BrokerUATRun[] }>("/api/trading/broker-uat-runs"),
    [],
  );
  const safetyState = useAsync(async () => {
    const [switches, audit] = await Promise.all([
      api.get<{ kill_switches: Array<{ scope_type: string; scope_id: string; active: boolean; reason?: string; updated_at?: string }> }>("/api/trading/kill-switches"),
      api.get<{ events: Array<Record<string, unknown>> }>("/api/trading/audit-events?limit=50"),
    ]);
    return { switches: switches.kill_switches, events: audit.events };
  }, []);
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
    if (!catalogsResolved || workspace === "paper") return;
    if (liveBrokerOptions.some((item) => item.name === runtimeBroker.trim())) return;
    const configured = (cfg?.trade_broker || cfg?.broker || "").trim();
    const eligibleConfigured = ["live", "shadow"].includes(workspace) && configured && configured !== "paper" ? liveBrokerOptions.find((item) => item.name === configured) : undefined;
    const fallback = liveBrokerOptions.find((item) => item.gateway_importable) || liveBrokerOptions[0];
    setRuntimeBroker((eligibleConfigured || fallback)?.name || "");
  }, [catalogsResolved, cfg?.broker, cfg?.trade_broker, liveBrokerOptions, runtimeBroker, workspace]);

  useEffect(() => {
    if (workspace === "paper" || !catalogsResolved) return;
    if (quoteProviderOptions.some((item) => item.name === runtimeQuoteProvider.trim())) return;
    const configured = (cfg?.quote_provider || "").trim();
    const eligibleConfigured = ["live", "shadow"].includes(workspace) && configured && configured !== "paper" ? quoteProviderOptions.find((item) => item.name === configured) : undefined;
    const matching = quoteProviderOptions.find((item) => item.name === runtimeBroker);
    const fallback = quoteProviderOptions.find((item) => item.gateway_importable) || quoteProviderOptions[0];
    setRuntimeQuoteProvider((eligibleConfigured || matching || fallback)?.name || "");
  }, [catalogsResolved, cfg?.quote_provider, quoteProviderOptions, runtimeBroker, runtimeQuoteProvider, workspace]);

  useEffect(() => {
    setPreflight(null);
  }, [runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider, preflightNetwork]);

  useEffect(() => {
    if (deploymentEvidence.data?.configuration?.account_profile) {
      setAccountProfile(deploymentEvidence.data.configuration.account_profile);
    }
  }, [deploymentEvidence.data?.configuration?.account_profile]);

  useEffect(() => {
    if (observeOnlyQuotes) setTargetRoute(false);
  }, [observeOnlyQuotes]);

  const switchWorkspace = (next: Workspace) => {
    if (next === workspace) return;
    setWorkspaceState(next);
    setTargetRoute(false);
    setOrderCode("");
    setRuntimeBroker("");
    setRuntimeQuoteProvider("");
    targetJson.setRaw('{\n  "holdings": {},\n  "prices": {}\n}');
    rememberWorkspace(next);
  };

  const closeDiagnostics = useCallback(() => setDiagnosticsOpen(false), []);

  const refreshWorkspace = useCallback(async () => {
    await Promise.all([
      daemonStatus.refresh(), runtimeState.refresh(), riskStatus.refresh(), ledgerEvents.refresh(),
      deploymentEvidence.refresh(), deploymentDiagnostics.refresh(), safetyState.refresh(),
      brokerUatRuns.refresh(),
    ]);
  }, [
    daemonStatus.refresh, runtimeState.refresh, riskStatus.refresh, ledgerEvents.refresh,
    deploymentEvidence.refresh, deploymentDiagnostics.refresh, safetyState.refresh,
    brokerUatRuns.refresh,
  ]);

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
    if (workspace === "paper") return;
    if (!(await confirm({ message: t("liveConnectConfirm"), danger: workspace === "live" }))) return;
    await run(async () => {
      await api.post<LiveConnectResult>("/api/live/runtime/connect", { ...query, timeout: 30 });
      await refreshWorkspace();
    }, t("liveRuntimeConnected"));
  };

  const startDaemon = async () => {
    if (!providerReady) return;
    if (workspace !== "paper" && (!selectedBrokerSpec?.gateway_importable || !selectedQuoteProviderSpec?.gateway_importable)) return;
    if (workspace === "live" && !(await confirm({ message: t("liveDaemonStartConfirm"), danger: true }))) return;
    await run(async () => {
      if (workspace === "paper" && (!Number.isFinite(Number(initialCash)) || Number(initialCash) <= 0)) {
        throw new Error("initial cash must be greater than 0");
      }
      await api.post("/api/live/daemon/start", {
        ...query,
        cash: workspace === "paper" ? Number(initialCash) || undefined : undefined,
        symbols: daemonSymbols,
        interval: 1,
        record_market_data: true,
        timeout: 30,
      });
      await refreshWorkspace();
    }, t("liveDaemonStarted"));
  };

  const stopDaemon = async () => {
    if (!(await confirm({ message: t("liveDaemonStopConfirm"), danger: true }))) return;
    await run(async () => {
      const result = await api.post<LiveDaemonStopResult>("/api/live/daemon/stop", { ...query, timeout: 5 });
      if (!result.stopped || result.alive || result.running) throw new Error(t("liveDaemonStopFailed"));
      await refreshWorkspace();
    }, t("liveDaemonStopped"));
  };

  const command = (path: string, message: string, payload: Record<string, unknown> = {}) => run(async () => {
    await api.post<LiveDaemonCommandResult>(path, { ...query, wait: true, timeout: 5, ...payload });
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
  const deploymentCommand = (action: string, message: string) => run(async () => {
    if (!daemonTimingStrategy.trim()) throw new Error(t("liveStrategyRequired"));
    await api.post(`/api/trading/deployments/${daemonTimingStrategy.trim()}/${action}`, {});
    await Promise.all([
      refreshWorkspace(), strategyInstances.refresh(), deploymentEvidence.refresh(),
      deploymentDiagnostics.refresh(),
    ]);
  }, message);
  const strategyPause = () => deploymentCommand("pause", t("liveStrategyPaused"));
  const strategyReconcile = () => deploymentCommand("reconcile", t("liveStrategyReconciled"));
  const strategyResume = () => deploymentCommand("resume", t("liveStrategyResumed"));
  const strategyStop = async () => {
    if (!(await confirm({ message: t("liveStrategyStopConfirm"), danger: true }))) return;
    await deploymentCommand("stop", t("liveStrategyStopped"));
  };
  const strategyStart = async () => {
    if (workspace === "live" && !(await confirm({ message: t("liveStrategyStartConfirm"), danger: true }))) return;
    if (workspace !== "paper") {
      const binding = deploymentEvidence.data?.configuration;
      if (
        !binding || binding.run_mode !== workspace
        || binding.trade_provider !== selectedRuntimeBroker
        || binding.quote_provider !== selectedRuntimeQuoteProvider
      ) {
        await run(async () => { throw new Error(t("liveBindingRequired")); });
        return;
      }
    }
    if (workspace === "paper" && deploymentEvidence.data?.configuration?.run_mode !== "paper") {
      await run(async () => { throw new Error(t("liveBindingRequired")); });
      return;
    }
    await deploymentCommand("start", t("liveStrategyStarted"));
  };

  const saveDeployment = () => run(async () => {
    if (!daemonTimingStrategy.trim()) throw new Error(t("liveStrategyRequired"));
    if (workspace === "simulation" && !accountProfile.trim()) throw new Error(t("liveAccountProfileRequired"));
    if ((workspace === "live" || workspace === "shadow") && !deploymentAccountId.trim()) {
      throw new Error(t("liveAccountIdRequired"));
    }
    await api.put(`/api/trading/deployments/${daemonTimingStrategy.trim()}`, {
      run_mode: workspace,
      trade_provider: workspace === "paper" ? "paper" : selectedRuntimeBroker,
      quote_provider: workspace === "paper" ? "paper" : selectedRuntimeQuoteProvider,
      account_profile: workspace === "simulation" ? accountProfile.trim() : "",
      account_id: workspace === "live" || workspace === "shadow" ? deploymentAccountId.trim() : "",
      reason: deploymentReason.trim() || undefined,
    });
    await Promise.all([deploymentEvidence.refresh(), deploymentDiagnostics.refresh()]);
  }, t("liveBindingSaved"));

  const changeKillSwitch = async (scopeType: string, scopeId: string, action: "engage" | "release") => {
    const reason = killReason.trim();
    if (!reason) {
      await run(async () => { throw new Error(t("liveKillReasonRequired")); });
      return;
    }
    if (!(await confirm({
      message: `${t(action === "engage" ? "liveKillEngage" : "liveKillRelease")} ${scopeType}:${scopeId} Kill Switch?`,
      danger: true,
    }))) return;
    await run(async () => {
      await api.post(`/api/trading/kill-switches/${scopeType}/${encodeURIComponent(scopeId)}/${action}`, { reason });
      await Promise.all([deploymentEvidence.refresh(), safetyState.refresh()]);
    }, t(action === "engage" ? "liveKillEngaged" : "liveKillReleased"));
  };

  const submitOrder = async () => {
    if (observeOnlyQuotes) return;
    if (!orderCode.trim()) return;
    const volume = Number(orderVolume);
    const price = Number(orderPrice);
    if (!Number.isInteger(volume) || volume <= 0) {
      await run(async () => { throw new Error("order volume must be a positive integer"); });
      return;
    }
    if (!Number.isFinite(price) || price < 0 || (orderType === "limit" && price <= 0)) {
      await run(async () => { throw new Error("limit order price must be greater than 0"); });
      return;
    }
    if (workspace === "live" && !(await confirm({ message: t("liveDaemonOrderConfirm"), danger: true }))) return;
    await command("/api/live/daemon/order", t("liveDaemonOrderDone"), {
      symbol: orderCode.trim(),
      side: orderSide,
      volume,
      price,
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
    if (targetRoute && observeOnlyQuotes) return;
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
  const bindingLocked = Boolean(deploymentEvidence.data?.runtime?.runtime_id);

  return (
    <div className="stack live-workspace-page">
      <PageTitle title={t("navLive")} subtitle={t("liveWorkspaceIntro")} />
      <section className="panel inset">
        <label className="field"><span>{t("liveOperatorTokenScope")}</span>
          <input type="password" value={operatorToken} onChange={(event) => {
            setOperatorTokenValue(event.target.value);
            setOperatorToken(event.target.value);
          }} placeholder="apop_…" autoComplete="off" />
        </label>
      </section>
      <div className="live-environment-tabs" role="tablist" aria-label={t("liveEnvironment")}>
        <button type="button" role="tab" aria-selected={workspace === "live"} className={workspace === "live" ? "active live" : ""} onClick={() => switchWorkspace("live")}>{t("liveEnvironmentLive")}</button>
        <button type="button" role="tab" aria-selected={workspace === "shadow"} className={workspace === "shadow" ? "active shadow" : ""} onClick={() => switchWorkspace("shadow")}>SHADOW</button>
        <button type="button" role="tab" aria-selected={workspace === "simulation"} className={workspace === "simulation" ? "active simulation" : ""} onClick={() => switchWorkspace("simulation")}>{t("liveEnvironmentSimulation")}</button>
        <button type="button" role="tab" aria-selected={workspace === "paper"} className={workspace === "paper" ? "active" : ""} onClick={() => switchWorkspace("paper")}>{t("liveEnvironmentPaper")}</button>
      </div>
      {workspace === "paper" ? (
        <div className="live-simulation-switch" role="group" aria-label={t("liveSimulationMode")}>
          <button type="button" className={simulationMode === "paper" ? "active" : ""} disabled={Boolean(daemon?.alive)} onClick={() => { setSimulationMode("paper"); setTargetRoute(false); }}>Paper</button>
          <button type="button" className={simulationMode === "dry_run" ? "active" : ""} disabled={Boolean(daemon?.alive)} onClick={() => { setSimulationMode("dry_run"); setTargetRoute(false); }}>Dry-run</button>
        </div>
      ) : null}
      {brokerCatalog.error || quoteProviderCatalog.error ? <Alert tone="error">{brokerCatalog.error || quoteProviderCatalog.error}</Alert> : null}
      {observeOnlyQuotes ? <Alert tone="info">{t("liveReplayQuoteWarning")}</Alert> : null}
      <LiveStatusBar workspace={workspace} runtimeMode={runtimeMode} tradeBroker={selectedRuntimeBroker} quoteProvider={selectedRuntimeQuoteProvider} daemon={daemon} onRefresh={refreshWorkspace} onOpenDiagnostics={() => setDiagnosticsOpen(true)} onHalt={haltDaemon} onResume={resumeDaemon} />
      <LiveProviderCard
        workspace={workspace}
        daemon={daemon}
        brokers={liveBrokerOptions}
        quoteProviders={quoteProviderOptions}
        runtimeBroker={runtimeBroker}
        setRuntimeBroker={setRuntimeBroker}
        runtimeQuoteProvider={runtimeQuoteProvider}
        setRuntimeQuoteProvider={setRuntimeQuoteProvider}
        symbols={daemonSymbols}
        setSymbols={setDaemonSymbols}
        initialCash={initialCash}
        setInitialCash={setInitialCash}
        providerReady={providerReady}
        providerSelectionLocked={Boolean(daemon?.alive)}
        canStartDaemon={canStartDaemon}
        preflight={preflight}
        preflightNetwork={preflightNetwork}
        setPreflightNetwork={setPreflightNetwork}
        onPreflight={checkRuntime}
        onConnect={connectRuntime}
        onStartDaemon={startDaemon}
        onRefreshDaemon={refreshDaemon}
        onReconnectDaemon={reconnectDaemon}
        onStopDaemon={stopDaemon}
      />
      {daemonTimingStrategy.trim() ? (
        <section className="panel inset" aria-label={t("liveExecutionBinding")}>
          <div className="panel-head"><div><h2>{t("liveExecutionBinding")}</h2><span className="muted">{t("liveExecutionBindingHint")}</span></div></div>
          {workspace === "paper" ? <Alert tone="info">{t("liveLocalPaperBindingHint")}</Alert> : (
            <>
              <Alert tone="info">{t("liveStrategyProviderBindingHint")}</Alert>
              <div className="live-runner-summary">
                <span><small>{t("liveTradeBroker")}</small><strong>{selectedRuntimeBroker || "—"}</strong></span>
                <span><small>{t("liveQuoteProvider")}</small><strong>{selectedRuntimeQuoteProvider || "—"}</strong></span>
                <span><small>{t("liveRunMode")}</small><strong>{workspace}</strong></span>
              </div>
              <div className="live-form-grid">
                {workspace === "simulation" ? <label className="field"><span>{t("liveAccountProfile")}</span><input value={accountProfile} onChange={(event) => setAccountProfile(event.target.value)} disabled={bindingLocked} placeholder="tts-uat" /></label> : null}
                {workspace === "live" || workspace === "shadow" ? <label className="field"><span>{t("liveAccountId")}</span><input type="password" value={deploymentAccountId} onChange={(event) => setDeploymentAccountId(event.target.value)} disabled={bindingLocked} autoComplete="off" /></label> : null}
              </div>
            </>
          )}
          <label className="field"><span>{t("liveDeploymentReason")}</span><input value={deploymentReason} onChange={(event) => setDeploymentReason(event.target.value)} disabled={bindingLocked} placeholder={t("liveDeploymentReasonHint")} /></label>
          <div className="row-actions"><button type="button" className="button small" disabled={bindingLocked || deploymentEvidence.loading || !providerReady} onClick={saveDeployment}>{t("save")}</button><span>{deploymentEvidence.data?.configuration?.run_mode || t("liveDeploymentUnconfigured")}</span><code>{deploymentEvidence.data?.configuration?.binding_hash?.slice(0, 12) || "—"}</code></div>
        </section>
      ) : null}
      <div className="live-workbench-grid">
        <LiveStrategyCard
          daemon={daemon}
          strategy={daemonTimingStrategy}
          setStrategy={setDaemonTimingStrategy}
          strategyNames={(strategyInstances.data?.instances || [])
            .filter((item) => item.validation_state === "validated")
            .map((item) => item.instance_id)}
          onStrategyStart={strategyStart}
          onStrategyPause={strategyPause}
          onStrategyReconcile={strategyReconcile}
          onStrategyResume={strategyResume}
          onStrategyStop={strategyStop}
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
          routeDisabled={observeOnlyQuotes}
          onSubmitOrder={submitOrder}
          onSubmitTarget={submitTarget}
        />
      </div>
      <section className="panel" aria-labelledby="deployment-safety-title">
        <div className="panel-head">
          <div>
            <h2 id="deployment-safety-title">{t("liveRuntimeSafety")}</h2>
            <span className="muted">{t("liveRuntimeSafetyHint")}</span>
          </div>
        </div>
        {deploymentEvidence.error || deploymentDiagnostics.error || safetyState.error || brokerUatRuns.error ? (
          <Alert tone="error">{deploymentEvidence.error || deploymentDiagnostics.error || safetyState.error || brokerUatRuns.error}</Alert>
        ) : null}
        {!daemonTimingStrategy.trim() ? <div className="empty">{t("liveRuntimeSafetyEmpty")}</div> : (
          <>
            <div className="live-runner-summary">
              <span><small>Desired / Observed</small><strong>{deploymentEvidence.data?.runtime?.desired_state || "—"} / {deploymentEvidence.data?.runtime?.observed_state || "—"}</strong></span>
              <span><small>{t("liveAccountProvider")}</small><strong>{deploymentEvidence.data?.runtime?.account_id_hash?.slice(0, 19) || "—"} / {deploymentEvidence.data?.runtime?.trade_provider || "—"}</strong></span>
              <span><small>{t("liveLastHeartbeat")}</small><strong>{deploymentEvidence.data?.runtime?.runner_heartbeat_at || "—"}</strong></span>
              <span><small>{t("liveNeedsReconcile")}</small><strong>{deploymentEvidence.data?.runtime?.reconcile_required ? t("liveYes") : t("liveNo")}</strong></span>
            </div>
            <div className="live-runner-summary" aria-label={t("liveRuntimeSafety")}>
              {Object.entries(deploymentDiagnostics.data?.modes || {}).map(([mode, summary]) => (
                <span key={mode}><small>{mode.toUpperCase()}</small><strong>{summary.trading_sessions || 0} {t("liveTradingDaysUnit")} / {summary.completed_runs || 0} {t("liveCompletedRunsUnit")}</strong></span>
              ))}
            </div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>{t("liveRunMode")}</th><th>{t("status")}</th><th>{t("liveTradingSessions")}</th><th>{t("liveConfigHash")}</th></tr></thead>
                <tbody>{(deploymentEvidence.data?.runs || []).map((item) => (
                  <tr key={item.run_id}><td>{item.run_mode}</td><td>{item.status}</td><td>{item.trading_sessions}</td><td><code>{item.config_hash.slice(0, 12)}</code></td></tr>
                ))}</tbody>
              </table>
            </div>
          </>
        )}
        <label className="field"><span>{t("liveKillReason")}</span>
          <input value={killReason} onChange={(event) => setKillReason(event.target.value)} placeholder={t("liveKillReasonHint")} />
        </label>
        <div className="row-actions">
          {daemonTimingStrategy.trim() ? (
            <button className="button danger" onClick={() => changeKillSwitch("instance", daemonTimingStrategy.trim(), "engage")}>{t("liveKillInstance")}</button>
          ) : null}
          {deploymentEvidence.data?.runtime?.account_id_hash ? (
            <button className="button danger" onClick={() => changeKillSwitch("account", String(deploymentEvidence.data?.runtime?.account_id_hash), "engage")}>{t("liveKillAccount")}</button>
          ) : null}
          <button className="button danger" onClick={() => changeKillSwitch("global", "*", "engage")}>{t("liveKillGlobal")}</button>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>{t("liveScope")}</th><th>{t("status")}</th><th>{t("reason")}</th><th>{t("action")}</th></tr></thead>
            <tbody>{(safetyState.data?.switches || []).map((item) => (
              <tr key={`${item.scope_type}:${item.scope_id}`}>
                <td>{item.scope_type}:{item.scope_id}</td><td>{item.active ? "ACTIVE" : "released"}</td><td>{item.reason || "—"}</td>
                <td>{item.active ? <button className="button small" onClick={() => changeKillSwitch(item.scope_type, item.scope_id, "release")}>{t("liveKillRelease")}</button> : "—"}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        <details>
          <summary>{t("liveRecentOperatorAudit")}</summary>
          <pre className="inline-json">{JSON.stringify((safetyState.data?.events || []).slice(0, 20), null, 2)}</pre>
        </details>
        <details>
          <summary>{t("liveUatEvidence")}</summary>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Broker</th><th>{t("liveUatEnvironment")}</th><th>{t("status")}</th><th>{t("liveUatStep")}</th><th>{t("liveUatSymbol")}</th><th>SDK</th><th>{t("expiresAt")}</th></tr></thead>
              <tbody>{(brokerUatRuns.data?.runs || []).map((item) => (
                <tr key={item.run_id}>
                  <td>{item.broker}</td><td>{item.environment || "—"}</td><td>{item.status}</td><td>{item.current_step || "—"}</td>
                  <td>{item.symbol}</td><td>{item.sdk_version || "—"}</td><td>{item.evidence?.expires_at || "—"}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          {!(brokerUatRuns.data?.runs || []).length ? <div className="empty">{t("liveUatEmpty")}</div> : null}
        </details>
      </section>
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
