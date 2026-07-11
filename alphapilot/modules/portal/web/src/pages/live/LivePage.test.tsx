import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { I18nProvider } from "../../i18n";
import { ToastProvider } from "../../toast";
import { LivePage } from "./LivePage";

function renderLivePage() {
  return render(
    <I18nProvider>
      <ToastProvider>
        <LivePage />
      </ToastProvider>
    </I18nProvider>,
  );
}

function mockLiveFetch(options: { alive?: boolean; accountMetrics?: boolean } = {}) {
  const alive = options.alive ?? true;
  const daemonState = {
    engine: {
      mode: "paper",
      halted: false,
      connection: "logged_in",
      session: "continuous",
      buying_power: 100000,
      active_orders: 1,
      positions: 0,
      contracts: 1,
      ticks: 1,
    },
    account: {
      balance: 100000,
      available: 99000,
      frozen: 0,
      ...(options.accountMetrics === false ? {} : { commission: 8.5, close_profit: 320, position_profit: 125, risk_ratio: 0.08 }),
    },
    positions: [{ code: "600000", exchange: "SSE", volume: 100, available: 0, yd_volume: 0, frozen: 0, price: 10, ...(options.accountMetrics === false ? {} : { pnl: 125 }) }],
    orders: [
      {
        order_id: "paper-active",
        code: "600000",
        exchange: "SSE",
        side: "buy",
        price: 10,
        volume: 100,
        traded: 0,
        status: "nottraded",
        active: true,
      },
    ],
    trades: [{ trade_id: "trade-1", code: "600000", side: "buy", price: 10, volume: 100 }],
  };
  const daemonStatus = {
    exists: true,
    path: "/tmp/daemon.json",
    alive,
    running: alive,
    status: alive ? "running" : "stopped",
    pid: 123,
    commands_processed: 2,
    runner: { enabled: true, strategy: "sma_filter", freq: "min" },
    runner_status: { enabled: true, active: true, paused: false, pending_requests: 0, algo_armed: false },
    last_command: { action: "strategy_start", ok: true },
    command_status_tail: [
      { ts: "2026-07-06T10:00:00", id: "cmd-1", action: "strategy_start", stage: "done", result: { ok: true, message: "strategy_started" } },
    ],
    state: daemonState,
  };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/live/status") {
      return Response.json({
        config: {
          mode: "paper",
          broker: "paper",
          trade_broker: "emt",
          quote_provider: "emt",
          timezone: "Asia/Shanghai",
          ledger_dir: "/tmp/ledger",
          state_dir: "/tmp/state",
          risk: {
            max_order_value: 200000,
            max_daily_value: 2000000,
            max_position_pct: 0.3,
            price_guard_pct: 0.05,
            lot_size: 100,
            max_orders_per_day: 1000,
          },
        },
        modes: ["dry_run", "paper", "live"],
        running: false,
      });
    }
    if (path === "/api/live/brokers") {
      return Response.json([
        {
          name: "emt",
          description: "东方财富证券 EMT",
          gateway: "alphapilot_broker_emt.factory:create_gateway",
          gateway_importable: true,
          plugin_id: "emt",
          distribution: "alphapilot-broker-emt",
          version: "0.1.0",
          env_fields: ["ALPHAPILOT_LIVE_EMT_ACCOUNT"],
          missing_env: [],
          capabilities: {
            asset_classes: ["stock"],
            supports_tick: true,
            supports_contract_query: true,
            supports_account_query: true,
            supports_position_query: true,
            supports_order_query: true,
            supports_trade_query: true,
            supports_cancel: true,
          },
        },
        {
          name: "xtp",
          description: "中泰证券 XTP PRO",
          gateway: "alphapilot_broker_xtp.factory:create_gateway",
          gateway_importable: false,
          env_fields: ["ALPHAPILOT_LIVE_XTP_ACCOUNT"],
          missing_env: ["ALPHAPILOT_LIVE_XTP_ACCOUNT"],
          capabilities: { supports_tick: true, supports_order_query: false, supports_trade_query: true, supports_cancel: true },
        },
      ]);
    }
    if (path === "/api/live/quote-providers") {
      return Response.json([
        {
          name: "paper",
          description: "Paper quote sandbox",
          gateway: "alphapilot.systems.live.brokers.paper:PaperBroker",
          gateway_importable: true,
          env_fields: [],
          missing_env: [],
          capabilities: { asset_classes: ["stock"], supports_tick: true },
        },
        {
          name: "emt",
          description: "东方财富证券 EMT",
          gateway: "alphapilot_broker_emt.factory:create_gateway",
          gateway_importable: true,
          env_fields: ["ALPHAPILOT_LIVE_EMT_ACCOUNT"],
          missing_env: [],
          capabilities: {
            asset_classes: ["stock"],
            supports_tick: true,
            supports_contract_query: true,
            supports_account_query: true,
            supports_position_query: true,
            supports_order_query: true,
            supports_trade_query: true,
            supports_cancel: true,
          },
        },
      ]);
    }
    if (path === "/api/live/plugins") {
      return Response.json({
        api_version: 1,
        entry_point_group: "alphapilot.live.plugins",
        plugins: [{ plugin_id: "emt", distribution: "alphapilot-broker-emt", version: "0.1.0", status: "loaded", providers: [{ name: "emt", roles: ["trade", "quote"] }] }],
        issues: [],
      });
    }
    if (path === "/api/timing/strategies") {
      return Response.json({ names: ["sma_filter", "boll_mean_reversion"], strategies: [] });
    }
    if (path.startsWith("/api/live/runtime/state")) {
      return Response.json({ exists: true, state_path: "/tmp/state/runtime_state.json", state: daemonState });
    }
    if (path.startsWith("/api/live/daemon/status")) return Response.json(daemonStatus);
    if (path.startsWith("/api/live/market/snapshot")) {
      return Response.json({
        exists: true,
        generated_at: "2026-07-06T10:02:03",
        quote_provider: "emt",
        daemon_running: true,
        daemon_status: "running",
        subscribed_symbols: ["600000.SSE"],
        stale_after_seconds: 3,
        ticks: [{
          key: "600000.SSE", code: "600000", exchange: "SSE", name: "浦发银行",
          last_price: 10.12, pre_close: 10, change: 0.12, change_pct: 1.2,
          bid_price_1: 10.11, ask_price_1: 10.12, bid_volume_1: 200, ask_volume_1: 300,
          volume: 120000, turnover: 1210000, datetime: "2026-07-06T10:02:02",
          received_at: "2026-07-06T10:02:03", age_seconds: 1, stale: false, gateway: "emt",
        }],
        recorder: {
          enabled: true, healthy: true, degraded: false, queue_depth: 0,
          written_ticks: 1234, written_bars: 20, dropped_ticks: 0, dropped_bars: 0,
        },
      });
    }
    if (path.startsWith("/api/live/market/bars")) {
      return Response.json({
        symbol: "600000.SSE", interval: path.includes("interval=300") ? 300 : 60,
        date_range: ["2026-07-06T10:01:00", "2026-07-06T10:02:00"],
        rows: [
          { date: "2026-07-06T10:01:00", open: 10, high: 10.1, low: 9.99, close: 10.08, volume: 1000, amount: 10080 },
          { date: "2026-07-06T10:02:00", open: 10.08, high: 10.12, low: 10.07, close: 10.12, volume: 800, amount: 8096 },
        ],
      });
    }
    if (path.startsWith("/api/live/risk/status")) {
      return Response.json({
        exists: true,
        state_path: "/tmp/state/runtime_state.json",
        ledger_dir: "/tmp/ledger",
        risk: {
          limits: { max_order_value: 200000, lot_size: 100 },
          orders_today: 1,
          value_today: 1000,
          seen_refs: ["ref-1"],
        },
        recovery: { risk_restored: true, warnings: [] },
        recent_rejections: [
          { ts: "2026-07-06T10:01:00", kind: "rejected", reference: "ref-2", payload: { rule: "duplicate", reason: "duplicate client reference" } },
        ],
      });
    }
    if (path.startsWith("/api/live/ledger/events")) {
      return Response.json({
        count: 1,
        events: [
          { ts: "2026-07-06T10:02:00", kind: "submit", source: "paper", order_id: "paper-1", reference: "ref-1", payload: { order_id: "paper-1" } },
        ],
      });
    }
    if (path === "/api/live/runtime/preflight" && init?.method === "POST") {
      const body = JSON.parse(String(init.body || "{}"));
      const trade = body.trade_broker || body.broker || "emt";
      const quote = body.quote_provider || trade;
      return Response.json({
        broker: trade,
        trade_broker: trade,
        quote_provider: quote,
        description: "东方财富证券 EMT",
        gateway_importable: true,
        missing_env: [],
        network_checked: Boolean(body.network),
        endpoints: body.network ? [{ name: "quote", host: "127.0.0.1", port: 1001, ok: true, detail: "reachable" }] : [],
        trade: {
          name: trade,
          broker: trade,
          gateway_importable: true,
          missing_env: [],
          network_checked: Boolean(body.network),
          endpoints: [],
          ok: true,
        },
        quote: {
          name: quote,
          broker: quote,
          gateway_importable: true,
          missing_env: [],
          network_checked: Boolean(body.network),
          endpoints: body.network ? [{ name: "quote", host: "127.0.0.1", port: 1001, ok: true, detail: "reachable" }] : [],
          ok: true,
        },
        ok: true,
      });
    }
    if (path === "/api/live/daemon/strategy/pause" && init?.method === "POST") {
      return Response.json({ accepted: true, daemon: { ...daemonStatus, runner_status: { enabled: true, active: false, paused: true } } });
    }
    if (path === "/api/live/daemon/start" && init?.method === "POST") {
      Object.assign(daemonStatus, { alive: true, running: true, status: "running" });
      return Response.json(daemonStatus);
    }
    if (path === "/api/live/daemon/reconnect" && init?.method === "POST") {
      return Response.json({ accepted: true, daemon: { ...daemonStatus, last_command: { action: "reconnect", ok: true } } });
    }
    if (path === "/api/live/daemon/cancel" && init?.method === "POST") {
      return Response.json({ accepted: true, daemon: { ...daemonStatus, last_command: { action: "cancel", ok: true } } });
    }
    if (path === "/api/live/daemon/order" && init?.method === "POST") {
      return Response.json({ accepted: true, daemon: { ...daemonStatus, last_command: { action: "order", ok: true, order_id: "paper-active" } } });
    }
    if (path === "/api/live/daemon/submit-target" && init?.method === "POST") {
      return Response.json({ accepted: true, daemon: { ...daemonStatus, last_command: { action: "target", ok: true, planned: 1 } } });
    }
    if (path === "/api/live/daemon/stop" && init?.method === "POST") {
      Object.assign(daemonStatus, { alive: false, running: false, status: "stopped" });
      return Response.json({ ...daemonStatus, stopped: true });
    }
    return Response.json({}, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function postedJson(fetchMock: ReturnType<typeof mockLiveFetch>, path: string) {
  const call = fetchMock.mock.calls.find(([input, init]) => String(input) === path && init?.method === "POST");
  if (!call) return null;
  return JSON.parse(String(call[1]?.body || "{}")) as Record<string, unknown>;
}

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LivePage", () => {
  it("defaults to Paper, renders one business tab at a time and opens an accessible diagnostics drawer", async () => {
    const fetchMock = mockLiveFetch();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderLivePage();

    expect(await screen.findByRole("tab", { name: "模拟 PAPER" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("策略运行")).toBeInTheDocument();
    expect(screen.getByText("手工交易")).toBeInTheDocument();
    expect(screen.queryByText("纸面演练")).not.toBeInTheDocument();
    expect((await screen.findAllByText("125")).length).toBeGreaterThan(0);
    expect(screen.getByRole("tab", { name: /持仓/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByText("trade-1")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /成交/ }));
    expect(await screen.findByText("trade-1")).toBeInTheDocument();
    expect(screen.queryByText("浮盈比例")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "连接与诊断" }));
    const drawer = await screen.findByRole("dialog", { name: "连接与诊断" });
    expect(drawer).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText("风控与恢复")).toBeInTheDocument();
    expect(screen.getAllByText("alphapilot-broker-emt").length).toBeGreaterThanOrEqual(1);
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "连接与诊断" })).not.toBeInTheDocument());

    const badLiveRequests = fetchMock.mock.calls.filter(([input]) => {
      const path = String(input);
      return path.includes("mode=live") && path.includes("trade_broker=paper");
    });
    expect(badLiveRequests).toHaveLength(0);
  });

  it("shows unavailable account and position metrics as em dashes", async () => {
    mockLiveFetch({ accountMetrics: false });
    renderLivePage();

    const status = await screen.findByRole("region", { name: "交易工作台状态" });
    expect(within(status).getAllByText("—").length).toBeGreaterThanOrEqual(3);
    expect(await screen.findByText("浮盈比例")).toBeInTheDocument();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(5);
  });

  it("remembers Live, preserves confirmations and controls daemon operations", async () => {
    window.localStorage.setItem("portal_live_workspace", "live");
    const fetchMock = mockLiveFetch();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderLivePage();

    expect(await screen.findByRole("tab", { name: "实盘 LIVE" })).toHaveAttribute("aria-selected", "true");

    fireEvent.click(screen.getByRole("button", { name: "连接与诊断" }));
    expect(await screen.findByLabelText("交易券商")).toBeDisabled();
    fireEvent.click(screen.getByLabelText("网络预检"));
    fireEvent.click(screen.getByRole("button", { name: "预检" }));
    await waitFor(() => expect(postedJson(fetchMock, "/api/live/runtime/preflight")?.network).toBe(true));
    expect(postedJson(fetchMock, "/api/live/runtime/preflight")?.trade_broker).toBe("emt");
    fireEvent.keyDown(document, { key: "Escape" });

    const code = screen.getByLabelText("代码");
    fireEvent.change(code, { target: { value: "SH600000" } });
    fireEvent.click(screen.getByRole("button", { name: "提交委托" }));
    await waitFor(() => expect(postedJson(fetchMock, "/api/live/daemon/order")?.confirm_live).toBe(true));
    expect(postedJson(fetchMock, "/api/live/daemon/order")?.symbol).toBe("SH600000");
    expect(postedJson(fetchMock, "/api/live/daemon/order")?.product).toBe("equity");

    fireEvent.click(screen.getByRole("tab", { name: "目标仓位" }));
    fireEvent.click(screen.getByLabelText("真实路由下单"));
    fireEvent.click(screen.getByRole("button", { name: "对账并下单" }));
    await waitFor(() => expect(postedJson(fetchMock, "/api/live/daemon/submit-target")?.confirm_live).toBe(true));
    expect(postedJson(fetchMock, "/api/live/daemon/submit-target")?.route).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "暂停策略" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input) === "/api/live/daemon/strategy/pause")).toBe(true));
    fireEvent.click(screen.getByText("更多技术操作"));
    fireEvent.click(screen.getByRole("button", { name: "重连" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input) === "/api/live/daemon/reconnect")).toBe(true));

    fireEvent.click(screen.getByRole("tab", { name: /^委托\s*1$/ }));
    fireEvent.click(screen.getByRole("button", { name: "撤单" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input) === "/api/live/daemon/cancel")).toBe(true));

    fireEvent.click(screen.getByRole("button", { name: "停止 daemon" }));
    await waitFor(() => expect(screen.getByRole("tab", { name: "模拟 PAPER" })).toBeEnabled());
    expect(fetchMock.mock.calls.some(([input]) => String(input) === "/api/live/daemon/stop")).toBe(true);
  });

  it("starts the Paper daemon with the selected initial cash", async () => {
    const fetchMock = mockLiveFetch({ alive: false });
    renderLivePage();

    expect(await screen.findByRole("button", { name: "启动 daemon" })).toBeEnabled();
    fireEvent.change(screen.getByLabelText("初始资金"), { target: { value: "250000" } });
    fireEvent.click(screen.getByRole("button", { name: "启动 daemon" }));
    await waitFor(() => expect(postedJson(fetchMock, "/api/live/daemon/start")?.mode).toBe("paper"));
    expect(postedJson(fetchMock, "/api/live/daemon/start")?.cash).toBe(250000);
    expect(postedJson(fetchMock, "/api/live/daemon/start")?.trade_broker).toBe("paper");
  });

  it("starts the Live daemon with resolved providers and no simulated cash", async () => {
    window.localStorage.setItem("portal_live_workspace", "live");
    const fetchMock = mockLiveFetch({ alive: false });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderLivePage();

    const start = await screen.findByRole("button", { name: "启动 daemon" });
    await waitFor(() => expect(start).toBeEnabled());
    fireEvent.click(start);
    await waitFor(() => expect(postedJson(fetchMock, "/api/live/daemon/start")?.mode).toBe("live"));
    expect(postedJson(fetchMock, "/api/live/daemon/start")?.trade_broker).toBe("emt");
    expect(postedJson(fetchMock, "/api/live/daemon/start")?.quote_provider).toBe("emt");
    expect(postedJson(fetchMock, "/api/live/daemon/start")?.cash).toBeUndefined();
  });

  it("persists environment changes and always resets target routing", async () => {
    mockLiveFetch({ alive: false });
    renderLivePage();

    await screen.findByText("手工交易");
    fireEvent.click(screen.getByRole("tab", { name: "目标仓位" }));
    const route = screen.getByLabelText("真实路由下单");
    fireEvent.click(route);
    expect(route).toBeChecked();
    fireEvent.click(screen.getByRole("tab", { name: "实盘 LIVE" }));
    await waitFor(() => expect(window.localStorage.getItem("portal_live_workspace")).toBe("live"));
    expect(screen.getByLabelText("真实路由下单")).not.toBeChecked();
    fireEvent.click(screen.getByRole("tab", { name: "普通委托" }));
    expect(screen.getByLabelText("代码")).toHaveValue("");
  });

  it("stops polling the previous environment after a workspace switch", async () => {
    const fetchMock = mockLiveFetch({ alive: false });
    renderLivePage();

    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).startsWith("/api/live/daemon/status?") && String(input).includes("mode=paper"))).toBe(true));
    fireEvent.click(screen.getByRole("tab", { name: "实盘 LIVE" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).startsWith("/api/live/daemon/status?") && String(input).includes("mode=live"))).toBe(true));
    const paperCalls = () => fetchMock.mock.calls.filter(([input]) => String(input).startsWith("/api/live/daemon/status?") && String(input).includes("mode=paper")).length;
    const countAfterSwitch = paperCalls();
    await act(async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 1150));
    });
    expect(paperCalls()).toBe(countAfterSwitch);
  });
});
