import { useCallback, useEffect, useMemo, useState } from "react";
import { api, getOperatorToken, qs, setOperatorToken } from "../../api";
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

type Workspace = "live" | "simulation" | "paper";
type SimulationMode = "paper" | "dry_run";
type Ticket = "order" | "target";

type DeploymentPackage = {
  instance?: { instance_id?: string; lifecycle?: string; deployment_level?: string; config_hash?: string };
  runtime?: {
    desired_state?: string; observed_state?: string; account_id_hash?: string; broker?: string;
    runtime_id?: string; runner_heartbeat_at?: string; reconcile_required?: boolean; last_error?: Record<string, unknown>;
    execution_environment?: string; trade_provider?: string; quote_provider?: string;
    quote_data_kind?: string; binding_hash?: string; reconciled?: boolean;
  };
  execution_binding?: ExecutionBinding;
  stage_runs?: Array<{
    run_id: string; stage: string; status: string; trading_sessions: number;
    config_hash: string; metrics?: Record<string, unknown>;
  }>;
  route_blocks?: Array<{ scope_type: string; scope_id: string; active: boolean; reason?: string }>;
};

type ExecutionBinding = {
  instance_id: string; execution_environment: "local_paper" | "broker_simulation" | "live";
  trade_provider: string; quote_provider: string; account_profile: string;
  quote_data_kind: "realtime" | "replay" | "synthetic"; binding_hash: string; version: number;
};

type QualificationPackage = {
  eligible_for_live_authorization: boolean;
  paper?: { passed: boolean; trading_sessions: number; minimum_sessions: number };
  shadow?: { passed: boolean; trading_sessions: number; minimum_sessions: number };
  parity?: { passed: boolean; passed_sessions: string[]; missing_sessions: string[] };
  broker_uat?: { required: boolean; passed: boolean; broker: string; expires_at?: string };
  reconcile?: { passed: boolean };
  configuration?: { passed: boolean; config_hash?: string };
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
    return stored === "live" || stored === "simulation" ? stored : "paper";
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
    () => api.get<{ instances: Array<{ instance_id: string; deployment_level: string; lifecycle: string; config?: { frequency?: string; universe?: string[] } }> }>("/api/trading/strategy-instances"),
    [],
  );

  const [workspace, setWorkspaceState] = useState<Workspace>(initialWorkspace);
  const [simulationMode, setSimulationMode] = useState<SimulationMode>("paper");
  const [runtimeBroker, setRuntimeBroker] = useState("");
  const [runtimeQuoteProvider, setRuntimeQuoteProvider] = useState("");
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [preflight, setPreflight] = useState<LivePreflight | null>(null);
  const [preflightNetwork, setPreflightNetwork] = useState(false);

  const runtimeMode = workspace === "live" ? "live" : workspace === "simulation" ? "simulation" : simulationMode;
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
      workspace === "live" ? (item.data_kind || "realtime") === "realtime"
      : workspace === "simulation" ? ["realtime", "replay"].includes(item.data_kind || "")
      : item.name === "paper"
    )),
    [quoteProviderCatalog.data, workspace],
  );
  const selectedBrokerSpec = liveBrokerOptions.find((item) => item.name === selectedRuntimeBroker);
  const selectedQuoteProviderSpec = quoteProviderOptions.find((item) => item.name === selectedRuntimeQuoteProvider);
  const catalogsResolved = !brokerCatalog.loading && !quoteProviderCatalog.loading && Boolean(brokerCatalog.data && quoteProviderCatalog.data);
  const providerReady = workspace === "paper" || (catalogsResolved && Boolean(selectedBrokerSpec && selectedQuoteProviderSpec));
  const observeOnlyQuotes = workspace === "simulation" && selectedQuoteProviderSpec?.data_kind !== "realtime";
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
  const executionBinding = useAsync(
    () => daemonTimingStrategy.trim()
      ? api.get<ExecutionBinding>(`/api/trading/deployments/${daemonTimingStrategy.trim()}/execution-binding`)
      : Promise.resolve(null as unknown as ExecutionBinding),
    [daemonTimingStrategy],
  );
  const [accountProfile, setAccountProfile] = useState("");
  const deploymentQualification = useAsync(
    () => daemonTimingStrategy.trim()
      ? api.get<QualificationPackage>(`/api/trading/deployments/${daemonTimingStrategy.trim()}/qualification`)
      : Promise.resolve({ eligible_for_live_authorization: false } as QualificationPackage),
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
    const eligibleConfigured = workspace === "live" && configured && configured !== "paper" ? liveBrokerOptions.find((item) => item.name === configured) : undefined;
    const fallback = liveBrokerOptions.find((item) => item.gateway_importable) || liveBrokerOptions[0];
    setRuntimeBroker((eligibleConfigured || fallback)?.name || "");
  }, [catalogsResolved, cfg?.broker, cfg?.trade_broker, liveBrokerOptions, runtimeBroker, workspace]);

  useEffect(() => {
    if (workspace === "paper" || !catalogsResolved) return;
    if (quoteProviderOptions.some((item) => item.name === runtimeQuoteProvider.trim())) return;
    const configured = (cfg?.quote_provider || "").trim();
    const eligibleConfigured = workspace === "live" && configured && configured !== "paper" ? quoteProviderOptions.find((item) => item.name === configured) : undefined;
    const matching = quoteProviderOptions.find((item) => item.name === runtimeBroker);
    const fallback = quoteProviderOptions.find((item) => item.gateway_importable) || quoteProviderOptions[0];
    setRuntimeQuoteProvider((eligibleConfigured || matching || fallback)?.name || "");
  }, [catalogsResolved, cfg?.quote_provider, quoteProviderOptions, runtimeBroker, runtimeQuoteProvider, workspace]);

  useEffect(() => {
    setPreflight(null);
  }, [runtimeMode, selectedRuntimeBroker, selectedRuntimeQuoteProvider, preflightNetwork]);

  useEffect(() => {
    if (executionBinding.data?.account_profile) setAccountProfile(executionBinding.data.account_profile);
  }, [executionBinding.data?.account_profile]);

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
      deploymentEvidence.refresh(), executionBinding.refresh(), safetyState.refresh(),
      deploymentQualification.refresh(), brokerUatRuns.refresh(),
    ]);
  }, [
    daemonStatus.refresh, runtimeState.refresh, riskStatus.refresh, ledgerEvents.refresh,
    deploymentEvidence.refresh, executionBinding.refresh, safetyState.refresh,
    deploymentQualification.refresh, brokerUatRuns.refresh,
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
    if (!daemonTimingStrategy.trim()) throw new Error("strategy instance is required");
    await api.post(`/api/trading/deployments/${daemonTimingStrategy.trim()}/${action}`, { reason: "portal" });
    await Promise.all([
      refreshWorkspace(), strategyInstances.refresh(), deploymentEvidence.refresh(),
      deploymentQualification.refresh(),
    ]);
  }, message);
  const strategyPause = () => deploymentCommand("pause", t("liveStrategyPaused"));
  const strategyReconcile = () => deploymentCommand("reconcile", "策略实例对账完成");
  const strategyResume = () => deploymentCommand("resume", t("liveStrategyResumed"));
  const strategyStop = async () => {
    if (!(await confirm({ message: t("liveStrategyStopConfirm"), danger: true }))) return;
    await deploymentCommand("stop", t("liveStrategyStopped"));
  };
  const strategyStart = async () => {
    if (workspace === "live" && !(await confirm({ message: t("liveStrategyStartConfirm"), danger: true }))) return;
    if (workspace === "simulation") {
      const binding = executionBinding.data;
      if (
        !binding || binding.execution_environment !== "broker_simulation"
        || binding.trade_provider !== selectedRuntimeBroker
        || binding.quote_provider !== selectedRuntimeQuoteProvider
      ) {
        await run(async () => { throw new Error(t("liveBindingRequired")); });
        return;
      }
    }
    if (workspace === "paper" && executionBinding.data?.execution_environment !== "local_paper") {
      await run(async () => { throw new Error(t("liveBindingRequired")); });
      return;
    }
    await deploymentCommand("start", t("liveStrategyStarted"));
  };

  const saveExecutionBinding = () => run(async () => {
    if (!daemonTimingStrategy.trim()) throw new Error("strategy instance is required");
    if (workspace === "simulation" && !accountProfile.trim()) throw new Error(t("liveAccountProfileRequired"));
    await api.put(`/api/trading/deployments/${daemonTimingStrategy.trim()}/execution-binding`, {
      execution_environment: workspace === "simulation" ? "broker_simulation" : "local_paper",
      trade_provider: workspace === "simulation" ? selectedRuntimeBroker : "paper",
      quote_provider: workspace === "simulation" ? selectedRuntimeQuoteProvider : "paper",
      account_profile: workspace === "simulation" ? accountProfile.trim() : "",
      reason: "portal execution binding change",
    });
    await Promise.all([executionBinding.refresh(), deploymentEvidence.refresh()]);
  }, t("liveBindingSaved"));

  const changeKillSwitch = async (scopeType: string, scopeId: string, action: "engage" | "release") => {
    const reason = killReason.trim();
    if (!reason) {
      await run(async () => { throw new Error("kill switch 操作必须填写原因"); });
      return;
    }
    if (!(await confirm({
      message: `${action === "engage" ? "启用" : "解除"} ${scopeType}:${scopeId} kill switch？`,
      danger: true,
    }))) return;
    await run(async () => {
      await api.post(`/api/trading/kill-switches/${scopeType}/${encodeURIComponent(scopeId)}/${action}`, { reason });
      await Promise.all([deploymentEvidence.refresh(), safetyState.refresh()]);
    }, action === "engage" ? "Kill switch 已启用" : "Kill switch 已解除");
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
        <label className="field"><span>操作员令牌（只保存在当前浏览器内存）</span>
          <input type="password" value={operatorToken} onChange={(event) => {
            setOperatorTokenValue(event.target.value);
            setOperatorToken(event.target.value);
          }} placeholder="apop_…" autoComplete="off" />
        </label>
      </section>
      <div className="live-environment-tabs" role="tablist" aria-label={t("liveEnvironment")}>
        <button type="button" role="tab" aria-selected={workspace === "live"} className={workspace === "live" ? "active live" : ""} onClick={() => switchWorkspace("live")}>{t("liveEnvironmentLive")}</button>
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
      {workspace !== "live" && daemonTimingStrategy.trim() ? (
        <section className="panel inset" aria-label={t("liveExecutionBinding")}>
          <div className="panel-head"><div><h2>{t("liveExecutionBinding")}</h2><span className="muted">{t("liveExecutionBindingHint")}</span></div></div>
          {workspace === "simulation" ? (
            <div className="live-form-grid">
              <label className="field"><span>{t("liveTradeBroker")}</span><select value={runtimeBroker} onChange={(event) => setRuntimeBroker(event.target.value)} disabled={bindingLocked}>{liveBrokerOptions.map((item) => <option key={item.name} value={item.name} disabled={!item.gateway_importable}>{item.name}</option>)}</select></label>
              <label className="field"><span>{t("liveQuoteProvider")}</span><select value={runtimeQuoteProvider} onChange={(event) => setRuntimeQuoteProvider(event.target.value)} disabled={bindingLocked}>{quoteProviderOptions.map((item) => <option key={item.name} value={item.name} disabled={!item.gateway_importable}>{item.name} ({item.data_kind})</option>)}</select></label>
              <label className="field"><span>{t("liveAccountProfile")}</span><input value={accountProfile} onChange={(event) => setAccountProfile(event.target.value)} disabled={bindingLocked} placeholder="tts-uat" /></label>
            </div>
          ) : <Alert tone="info">{t("liveLocalPaperBindingHint")}</Alert>}
          <div className="row-actions"><button type="button" className="button small" disabled={bindingLocked || executionBinding.loading || !providerReady} onClick={saveExecutionBinding}>{t("save")}</button><code>{executionBinding.data?.binding_hash?.slice(0, 12) || "—"}</code></div>
        </section>
      ) : null}
      <div className="live-workbench-grid">
        <LiveStrategyCard
          daemon={daemon}
          symbols={daemonSymbols}
          setSymbols={setDaemonSymbols}
          strategy={daemonTimingStrategy}
          setStrategy={setDaemonTimingStrategy}
          strategyNames={(strategyInstances.data?.instances || [])
            .filter((item) => item.deployment_level === (workspace === "live" ? "live" : "paper"))
            .map((item) => item.instance_id)}
          initialCash={initialCash}
          setInitialCash={setInitialCash}
          simulated={workspace === "paper"}
          canStartDaemon={canStartDaemon}
          onStartDaemon={startDaemon}
          onStrategyStart={strategyStart}
          onStrategyPause={strategyPause}
          onStrategyReconcile={strategyReconcile}
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
          routeDisabled={observeOnlyQuotes}
          onSubmitOrder={submitOrder}
          onSubmitTarget={submitTarget}
        />
      </div>
      <section className="panel" aria-labelledby="deployment-safety-title">
        <div className="panel-head">
          <div>
            <h2 id="deployment-safety-title">部署证据与安全控制</h2>
            <span className="muted">状态以 trading runtime 为准；daemon 心跳、对账和配置哈希不一致时自动路由关闭。</span>
          </div>
        </div>
        {deploymentEvidence.error || deploymentQualification.error || safetyState.error || brokerUatRuns.error ? (
          <Alert tone="error">{deploymentEvidence.error || deploymentQualification.error || safetyState.error || brokerUatRuns.error}</Alert>
        ) : null}
        {!daemonTimingStrategy.trim() ? <div className="empty">选择策略实例后查看部署、阶段证据和 kill switch。</div> : (
          <>
            <div className="live-runner-summary">
              <span><small>Desired / Observed</small><strong>{deploymentEvidence.data?.runtime?.desired_state || "—"} / {deploymentEvidence.data?.runtime?.observed_state || "—"}</strong></span>
              <span><small>账户哈希 / Broker</small><strong>{deploymentEvidence.data?.runtime?.account_id_hash?.slice(0, 19) || "—"} / {deploymentEvidence.data?.runtime?.broker || "—"}</strong></span>
              <span><small>最近心跳</small><strong>{deploymentEvidence.data?.runtime?.runner_heartbeat_at || "—"}</strong></span>
              <span><small>需要对账</small><strong>{deploymentEvidence.data?.runtime?.reconcile_required ? "是" : "否"}</strong></span>
            </div>
            <div className="live-runner-summary" aria-label="LIVE qualification">
              <span><small>LIVE 资格</small><strong>{deploymentQualification.data?.eligible_for_live_authorization ? "PASS" : "BLOCKED"}</strong></span>
              <span><small>PAPER</small><strong>{deploymentQualification.data?.paper?.trading_sessions ?? 0} / {deploymentQualification.data?.paper?.minimum_sessions ?? 20}</strong></span>
              <span><small>SHADOW</small><strong>{deploymentQualification.data?.shadow?.trading_sessions ?? 0} / {deploymentQualification.data?.shadow?.minimum_sessions ?? 5}</strong></span>
              <span><small>逐日 Parity</small><strong>{deploymentQualification.data?.parity?.passed ? "PASS" : `缺 ${deploymentQualification.data?.parity?.missing_sessions?.length ?? 0} 日`}</strong></span>
              <span><small>Broker UAT</small><strong>{deploymentQualification.data?.broker_uat?.required ? (deploymentQualification.data?.broker_uat?.passed ? "PASS" : "BLOCKED") : "N/A"}</strong></span>
            </div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>阶段</th><th>状态</th><th>交易日</th><th>配置哈希</th></tr></thead>
                <tbody>{(deploymentEvidence.data?.stage_runs || []).map((item) => (
                  <tr key={item.run_id}><td>{item.stage}</td><td>{item.status}</td><td>{item.trading_sessions}</td><td><code>{item.config_hash.slice(0, 12)}</code></td></tr>
                ))}</tbody>
              </table>
            </div>
          </>
        )}
        <label className="field"><span>Kill switch 操作原因</span>
          <input value={killReason} onChange={(event) => setKillReason(event.target.value)} placeholder="必填，并写入操作员审计" />
        </label>
        <div className="row-actions">
          {daemonTimingStrategy.trim() ? (
            <button className="button danger" onClick={() => changeKillSwitch("instance", daemonTimingStrategy.trim(), "engage")}>实例级停止新单</button>
          ) : null}
          {deploymentEvidence.data?.runtime?.account_id_hash ? (
            <button className="button danger" onClick={() => changeKillSwitch("account", String(deploymentEvidence.data?.runtime?.account_id_hash), "engage")}>账户级停止新单</button>
          ) : null}
          <button className="button danger" onClick={() => changeKillSwitch("global", "*", "engage")}>全局停止新单</button>
        </div>
        <div className="table-wrap">
          <table>
            <thead><tr><th>范围</th><th>状态</th><th>原因</th><th>操作</th></tr></thead>
            <tbody>{(safetyState.data?.switches || []).map((item) => (
              <tr key={`${item.scope_type}:${item.scope_id}`}>
                <td>{item.scope_type}:{item.scope_id}</td><td>{item.active ? "ACTIVE" : "released"}</td><td>{item.reason || "—"}</td>
                <td>{item.active ? <button className="button small" onClick={() => changeKillSwitch(item.scope_type, item.scope_id, "release")}>解除</button> : "—"}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        <details>
          <summary>最近操作员审计</summary>
          <pre className="inline-json">{JSON.stringify((safetyState.data?.events || []).slice(0, 20), null, 2)}</pre>
        </details>
        <details>
          <summary>XTP / EMT UAT 证据（只读）</summary>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Broker</th><th>环境</th><th>状态</th><th>步骤</th><th>标的</th><th>SDK</th><th>到期</th></tr></thead>
              <tbody>{(brokerUatRuns.data?.runs || []).map((item) => (
                <tr key={item.run_id}>
                  <td>{item.broker}</td><td>{item.environment || "—"}</td><td>{item.status}</td><td>{item.current_step || "—"}</td>
                  <td>{item.symbol}</td><td>{item.sdk_version || "—"}</td><td>{item.evidence?.expires_at || "—"}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          {!(brokerUatRuns.data?.runs || []).length ? <div className="empty">尚无真实券商 UAT 证据；只能通过本地 CLI 运行。</div> : null}
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
