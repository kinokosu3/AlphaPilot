import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

function mockLiveFetch() {
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
    account: { balance: 100000, available: 99000, frozen: 0 },
    positions: [{ code: "600000", exchange: "SSE", volume: 100, available: 0, yd_volume: 0, frozen: 0, price: 10 }],
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
    alive: true,
    running: true,
    status: "running",
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
          gateway: "alphapilot.systems.live.brokers.emt:EmtGateway",
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
        {
          name: "xtp",
          description: "中泰证券 XTP PRO",
          gateway: "alphapilot.systems.live.brokers.xtp_pro:XtpProGateway",
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
          gateway: "alphapilot.systems.live.brokers.emt:EmtGateway",
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
    if (path.startsWith("/api/live/runtime/state")) {
      return Response.json({ exists: true, state_path: "/tmp/state/runtime_state.json", state: daemonState });
    }
    if (path.startsWith("/api/live/daemon/status")) return Response.json(daemonStatus);
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
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LivePage", () => {
  it("shows risk and ledger panels and controls daemon operations", async () => {
    const fetchMock = mockLiveFetch();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderLivePage();

    expect(await screen.findByText("风控与恢复")).toBeInTheDocument();
    expect(await screen.findByText("审计事件")).toBeInTheDocument();
    expect(await screen.findByText("ref-1")).toBeInTheDocument();
    expect(await screen.findByText("daemon 手动委托")).toBeInTheDocument();
    expect(await screen.findByText("trade-1")).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("网络预检"));
    fireEvent.click(screen.getByRole("button", { name: "预检" }));
    await waitFor(() => {
      expect(postedJson(fetchMock, "/api/live/runtime/preflight")?.network).toBe(true);
    });
    expect(postedJson(fetchMock, "/api/live/runtime/preflight")?.broker).toBe("emt");
    expect(postedJson(fetchMock, "/api/live/runtime/preflight")?.trade_broker).toBe("emt");
    expect(postedJson(fetchMock, "/api/live/runtime/preflight")?.quote_provider).toBe("emt");

    fireEvent.click(screen.getByRole("button", { name: "提交委托" }));
    await waitFor(() => {
      expect(postedJson(fetchMock, "/api/live/daemon/order")?.confirm_live).toBe(true);
    });
    expect(postedJson(fetchMock, "/api/live/daemon/order")?.symbol).toBe("SH600000");
    expect(postedJson(fetchMock, "/api/live/daemon/order")?.product).toBe("equity");
    expect(postedJson(fetchMock, "/api/live/daemon/order")?.offset).toBe("none");

    fireEvent.click(screen.getByLabelText("真实路由下单"));
    fireEvent.click(screen.getByRole("button", { name: "对账并下单" }));
    await waitFor(() => {
      expect(postedJson(fetchMock, "/api/live/daemon/submit-target")?.confirm_live).toBe(true);
    });
    expect(postedJson(fetchMock, "/api/live/daemon/submit-target")?.route).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "暂停策略" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input, init]) => String(input) === "/api/live/daemon/strategy/pause" && init?.method === "POST")).toBe(true);
    });

    fireEvent.click(screen.getByRole("button", { name: "重连" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input, init]) => String(input) === "/api/live/daemon/reconnect" && init?.method === "POST")).toBe(true);
    });

    fireEvent.click(screen.getByRole("button", { name: "撤单" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([input, init]) => String(input) === "/api/live/daemon/cancel" && init?.method === "POST")).toBe(true);
    });
  });
});
