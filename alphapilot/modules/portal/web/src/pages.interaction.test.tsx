import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { I18nProvider } from "./i18n";
import { AdvancedPage, BacktestPage, DailyTradePage, MarketPage, MiningPage, NotificationsPage, SchedulerPage, TimingPage } from "./pages";
import { ToastProvider } from "./toast";

vi.mock("react-plotly.js", () => ({ default: () => null }));

function renderPage(page: ReactNode) {
  return render(<I18nProvider><ToastProvider>{page}</ToastProvider></I18nProvider>);
}

function bodyOf(call: [RequestInfo | URL, RequestInit?]) {
  return JSON.parse(String(call[1]?.body || "{}")) as Record<string, unknown>;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("MarketPage interaction state", () => {
  it("stops polling immediately after a data job reaches a terminal state", async () => {
    let progressCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/market/sources") return Response.json([]);
      if (path === "/api/jobs" && init?.method === "POST") return Response.json({ job_id: "data-job", kind: "data", status: "running" });
      if (path === "/api/jobs") return Response.json([]);
      if (path === "/api/jobs/data-job/progress") {
        progressCalls += 1;
        return Response.json({ job_id: "data-job", status: "succeeded", percent: 100, stage: "done" });
      }
      if (path === "/api/modules/run" && init?.method === "POST") return Response.json([]);
      if (path.startsWith("/api/data/symbols")) return Response.json({});
      return Response.json({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPage(<MarketPage />);

    await screen.findByRole("heading", { name: "数据动作" });
    await user.click(screen.getByRole("button", { name: "运行" }));
    await waitFor(() => expect(progressCalls).toBe(1));
    await new Promise((resolve) => window.setTimeout(resolve, 2150));
    expect(progressCalls).toBe(1);
  });

  it("keeps stock-pool create, detail and member updates consistent", async () => {
    let created = false;
    let members = ["600000.SH"];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/market/sources" || path === "/api/jobs") return Response.json([]);
      if (path.startsWith("/api/data/symbols")) return Response.json({});
      if (path === "/api/modules/run" && init?.method === "POST") {
        const payload = bodyOf([input, init]);
        if (payload.command === "pool_list") return Response.json(created ? [{ name: "qa-pool", count: members.length, description: "QA" }] : []);
        if (payload.command === "pool_create") { created = true; return Response.json({ name: "qa-pool", count: 1, symbols: members, invalid: [], missing_data: [] }); }
        if (payload.command === "pool_show") return Response.json({ name: "qa-pool", description: "QA", symbols: members });
        if (payload.command === "pool_add") { members = [...members, "600085.SH"]; return Response.json({ name: "qa-pool", count: 2, symbols: members, invalid: [], missing_data: [] }); }
        return Response.json({});
      }
      return Response.json({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPage(<MarketPage />);

    await user.type(await screen.findByLabelText("名称"), "qa-pool");
    await user.type(screen.getByLabelText(/^股票代码（批量）/), "600000.SH");
    await user.click(screen.getByRole("button", { name: "创建股票池" }));
    await screen.findByRole("heading", { name: /qa-pool.*成员/ });
    await user.type(screen.getByLabelText("增加股票"), "600085.SH");
    await user.click(screen.getByRole("button", { name: "增加" }));
    await screen.findByText("600085.SH");
    expect(fetchMock.mock.calls.filter(([path, init]) => String(path) === "/api/modules/run" && init?.method === "POST").length).toBeGreaterThanOrEqual(6);
  });
});

describe("MiningPage latest-selection behavior", () => {
  it("preserves user-entered direction while stock-pool options load asynchronously", async () => {
    let resolveSets: ((response: Response) => void) | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/data/instrument-sets") return new Promise<Response>((resolve) => { resolveSets = resolve; });
      if (path === "/api/mining/sessions" || path === "/api/jobs") return Promise.resolve(Response.json([]));
      return Promise.resolve(Response.json({}, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPage(<MiningPage />);
    const direction = screen.getByLabelText(/方向 Direction/);
    await user.type(direction, "用户已填写的量价方向");
    resolveSets?.(Response.json({ sets: ["qa_stock_pool_30"] }));
    expect((await screen.findAllByRole("option", { name: "qa_stock_pool_30" })).length).toBeGreaterThan(0);
    expect(direction).toHaveValue("用户已填写的量价方向");
  });

  it("does not allow a slow old session response to replace the latest session", async () => {
    const resolvers = new Map<string, (response: Response) => void>();
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/data/instrument-sets") return Promise.resolve(Response.json({ sets: ["qa_stock_pool_30"] }));
      if (path === "/api/mining/sessions") return Promise.resolve(Response.json([
        { name: "old-session", path: "/old" }, { name: "new-session", path: "/new" },
      ]));
      if (path === "/api/jobs") return Promise.resolve(Response.json([]));
      if (path.includes("/api/mining/sessions/")) return new Promise<Response>((resolve) => resolvers.set(path, resolve));
      return Promise.resolve(Response.json({}, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPage(<MiningPage />);

    const oldRow = (await screen.findByText("old-session")).closest("tr") as HTMLElement;
    const newRow = screen.getByText("new-session").closest("tr") as HTMLElement;
    await user.click(within(oldRow).getByRole("button", { name: "打开" }));
    await user.click(within(newRow).getByRole("button", { name: "打开" }));
    resolvers.get("/api/mining/sessions/new-session")?.(Response.json({ name: "new-session", path: "/new", files: [] }));
    await waitFor(() => expect(screen.getAllByText(/new-session/).length).toBeGreaterThan(1));
    resolvers.get("/api/mining/sessions/old-session")?.(Response.json({ name: "old-session", path: "/old", files: [] }));
    await Promise.resolve();
    expect(screen.getAllByText(/new-session/).length).toBeGreaterThan(1);
    expect(screen.getAllByText(/old-session/)).toHaveLength(1);
  });
});

describe("BacktestPage latest-selection behavior", () => {
  it("keeps the newest workspace visible when an older detail resolves last", async () => {
    const resolvers = new Map<string, (response: Response) => void>();
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/data/instrument-sets") return Promise.resolve(Response.json({ sets: [] }));
      if (path === "/api/strategies") return Promise.resolve(Response.json({ names: [] }));
      if (path === "/api/backtests") return Promise.resolve(Response.json([
        { workspace_id: "old-workspace", label: "Old result" },
        { workspace_id: "new-workspace", label: "New result" },
      ]));
      if (path === "/api/backtests/leaderboards" || path === "/api/jobs") return Promise.resolve(Response.json([]));
      if (path.startsWith("/api/backtests/")) return new Promise<Response>((resolve) => resolvers.set(path, resolve));
      return Promise.resolve(Response.json({}, { status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPage(<BacktestPage />);

    const oldRow = (await screen.findByText("Old result")).closest("tr") as HTMLElement;
    const newRow = screen.getByText("New result").closest("tr") as HTMLElement;
    await user.click(within(oldRow).getByRole("button", { name: "打开" }));
    await user.click(within(newRow).getByRole("button", { name: "打开" }));
    resolvers.get("/api/backtests/new-workspace")?.(Response.json({ workspace_id: "new-workspace", report: [], trades: [], holdings: [] }));
    await screen.findByRole("heading", { name: "new-workspace" });
    resolvers.get("/api/backtests/old-workspace")?.(Response.json({ workspace_id: "old-workspace", report: [], trades: [], holdings: [] }));
    await Promise.resolve();
    expect(screen.queryByRole("heading", { name: "old-workspace" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "new-workspace" })).toBeInTheDocument();
  });
});

describe("DailyTradePage run-mode contract", () => {
  it("never leaks one-off strategy or initial cash into a session run", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/strategies") return Response.json({ names: ["qa-strategy"], strategies: [] });
      if (path === "/api/trade-sessions") return Response.json([{ name: "qa-session", source_strategy: "qa-strategy", status: "ready" }]);
      if (path === "/api/trade-sessions/qa-session") return Response.json({ manifest: { name: "qa-session", source_strategy: "qa-strategy", init_cash: 500000 }, state: { cash: 500000, positions: {} }, cashflows: [] });
      if (path === "/api/daily-trade" && init?.method === "POST") return Response.json({ job_id: "daily-job", kind: "daily_signals", status: "failed" });
      if (path === "/api/jobs/daily-job/progress") return Response.json({ job_id: "daily-job", status: "failed", percent: 100, stage: "failed" });
      if (path === "/api/jobs") return Response.json([]);
      return Response.json({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPage(<DailyTradePage />);

    const runMode = await screen.findByLabelText("运行模式");
    await user.selectOptions(runMode, "qa-session");
    await screen.findByText(/现金/);
    await user.click(screen.getByRole("button", { name: "运行该会话" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => String(path) === "/api/daily-trade" && init?.method === "POST")).toBe(true));
    const call = fetchMock.mock.calls.find(([path, init]) => String(path) === "/api/daily-trade" && init?.method === "POST") as [RequestInfo | URL, RequestInit?];
    const payload = bodyOf(call);
    expect(payload.session).toBe("qa-session");
    expect(payload).not.toHaveProperty("strategy_name");
    expect(payload).not.toHaveProperty("init_cash");
  });
});

describe("TimingPage merged-parameter validation", () => {
  it("rejects contradictory advanced strategy windows before either endpoint is called", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/trading/strategy-definitions") return Response.json({
        definitions: [{ strategy_id: "dual_ma", version: "1.0.0", signal_kind: "instrument_timing", description: "Dual MA", parameter_schema: { properties: { short_window: { default: 5 }, long_window: { default: 20 } } } }],
      });
      if (path === "/api/trading/strategy-instances") return Response.json({ instances: [{ instance_id: "dual_ma_demo", strategy_id: "dual_ma", validation_state: "validated", config_hash: "abc" }] });
      if (path === "/api/jobs") return Response.json([]);
      return Response.json({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPage(<TimingPage />);
    await screen.findByText("Dual MA");
    await user.selectOptions(screen.getByLabelText("运行实例"), "dual_ma_demo");
    const advanced = screen.getByText("高级 JSON").closest("details")?.querySelector("textarea") as HTMLTextAreaElement;
    await user.clear(advanced);
    await user.click(advanced);
    await user.paste('{"strategy_params":{"short_window":20,"long_window":5}}');
    await user.click(screen.getByRole("button", { name: "预览信号" }));
    await screen.findByText("strategy_params.short_window must be less than strategy_params.long_window");
    expect(fetchMock.mock.calls.some(([path]) => String(path).endsWith("/preview"))).toBe(false);
  });
});

describe("SchedulerPage parameter isolation", () => {
  it("drops hidden data-download fields after switching to an LLM schedule", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/schedules" && init?.method === "POST") return Response.json({ schedule_id: "schedule-1" });
      if (path === "/api/schedules") return Response.json([]);
      if (path === "/api/schedules/daemon") return Response.json({ running: false });
      if (path === "/api/strategies") return Response.json({ names: ["qa-strategy"] });
      if (path === "/api/data/instrument-sets") return Response.json({ sets: ["qa_stock_pool_30"] });
      return Response.json({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPage(<SchedulerPage />);

    const type = await screen.findByLabelText("类型");
    await user.selectOptions(type, "mine");
    await user.type(screen.getByLabelText(/方向 Direction/), "量价反转");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => String(path) === "/api/schedules" && init?.method === "POST")).toBe(true));
    const call = fetchMock.mock.calls.find(([path, init]) => String(path) === "/api/schedules" && init?.method === "POST") as [RequestInfo | URL, RequestInit?];
    const kwargs = bodyOf(call).kwargs as Record<string, unknown>;
    expect(kwargs.direction).toBe("量价反转");
    expect(kwargs).not.toHaveProperty("action");
    expect(kwargs).not.toHaveProperty("source");
    expect(kwargs).not.toHaveProperty("adjust_mode");
  });
});

describe("NotificationsPage secret handling", () => {
  it("keeps a configured secret masked and submits the preservation marker", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/notify" && init?.method === "PATCH") return Response.json({ saved: true });
      if (path === "/api/notify") return Response.json({
        config: { telegram: { bot_token: "********" }, options: { notify_on_all_jobs: false } },
        fields: { telegram: [["bot_token", "secret"]] },
        configured_channels: ["telegram"],
        credentials_path: "/isolated/notify.json",
        masked_secret: "********",
      });
      if (path === "/api/notify/commands/status") return Response.json({ daemon: { running: false }, events: [] });
      if (path.startsWith("/api/notify/test") && init?.method === "POST") return Response.json({ ok: true });
      if (path === "/api/notify/commands/plan" && init?.method === "POST") return Response.json({ action: "jobs" });
      if (path === "/api/notify/commands/start" && init?.method === "POST") return Response.json({ started: true });
      if (path === "/api/notify/commands/pair-code" && init?.method === "POST") return Response.json({ code: "123456", expires_at: "2026-07-11T13:00:00" });
      if (path === "/api/notify/commands/register-menu" && init?.method === "POST") return Response.json({ registered: true });
      return Response.json({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();
    renderPage(<NotificationsPage />);

    await user.click(await screen.findByText("telegram"));
    const token = screen.getByLabelText("bot_token");
    expect(token).toHaveValue("");
    expect(token).toHaveAttribute("placeholder", "已配置 - 留空表示保持不变");
    const section = screen.getByRole("heading", { name: "发送渠道" }).closest("section") as HTMLElement;
    await user.click(within(section).getByRole("button", { name: "保存" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => String(path) === "/api/notify" && init?.method === "PATCH")).toBe(true));
    const call = fetchMock.mock.calls.find(([path, init]) => String(path) === "/api/notify" && init?.method === "PATCH") as [RequestInfo | URL, RequestInit?];
    expect((((bodyOf(call).config as Record<string, unknown>).telegram as Record<string, unknown>).bot_token)).toBe("********");
    await user.click(screen.getByLabelText("所有后台任务完成后通知"));
    await user.click(within(section).getByRole("button", { name: "测试 telegram" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "仅规划" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "仅规划" }));
    await screen.findByText(/"action": "jobs"/);
    await user.click(screen.getByRole("button", { name: "生成配对码" }));
    await screen.findByText("123456");
    await user.click(screen.getByRole("button", { name: "注册命令菜单" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "启动接收器" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "启动接收器" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => String(path) === "/api/notify/commands/start")).toBe(true));
  });
});

describe("AdvancedPage destructive action separation", () => {
  it("sends preview with execute=false and cancellation never sends execute=true", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/portal/settings") return Response.json({
        settings: { host: "127.0.0.1", port: 19901, timezone: "Asia/Shanghai" },
        current: { host: "127.0.0.1", port: 19901, timezone: "Asia/Shanghai" },
        config_path: "/isolated/portal.json", host_options: [{ value: "127.0.0.1", label: "local" }],
        timezone_options: ["Asia/Shanghai"], restart_required: false,
      });
      if (path === "/api/portal/env") return Response.json({
        fields: [{ key: "OPENAI_API_KEY", label: "LLM key", group: "LLM", kind: "password", secret: true, requires_restart: true }],
        values: { OPENAI_API_KEY: "********" }, current: { OPENAI_API_KEY: "********" },
        config_path: "/isolated/.env", restart_required: false, restart_required_keys: [], masked_secret: "********",
      });
      if (path === "/api/modules") return Response.json({ portal: { commands: [{ name: "scheduler" }] } });
      if (path === "/api/logs/cleanup" && init?.method === "POST") return Response.json({ log_root: "/isolated/log", execute: bodyOf([input, init]).execute, removed: 0, paths: ["/isolated/log/stub"] });
      return Response.json({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    renderPage(<AdvancedPage />);

    const section = (await screen.findByRole("heading", { name: "日志清理" })).closest("section") as HTMLElement;
    expect(screen.getByPlaceholderText("已配置 - 留空表示保持不变")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "保存环境设置" }));
    await user.click(screen.getByRole("button", { name: "保存门户设置" }));
    await user.click(within(section).getByRole("button", { name: "预览" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path, init]) => String(path) === "/api/logs/cleanup" && init?.method === "POST")).toBe(true));
    await user.click(within(section).getByRole("button", { name: "删除" }));
    const calls = fetchMock.mock.calls.filter(([path, init]) => String(path) === "/api/logs/cleanup" && init?.method === "POST");
    expect(calls).toHaveLength(1);
    expect(bodyOf(calls[0] as [RequestInfo | URL, RequestInit?]).execute).toBe(false);
    const envCall = fetchMock.mock.calls.find(([path, init]) => String(path) === "/api/portal/env" && init?.method === "PATCH") as [RequestInfo | URL, RequestInit?];
    expect((bodyOf(envCall).values as Record<string, unknown>).OPENAI_API_KEY).toBe("");
  });
});
