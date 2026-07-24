import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { I18nProvider } from "./i18n";
import { klineAxisType, klineCategoryTicks, klineIsIntraday, klineTimeLabel, LibraryPage, TimingPage } from "./pages";
import { ToastProvider } from "./toast";

vi.mock("react-plotly.js", () => ({ default: () => null }));

const klineRow = (date: string) => ({
  date, open: 1, high: 1, low: 1, close: 1, volume: 1, amount: 1, turn: 1, pctChg: 0,
});

describe("kline axis selection", () => {
  it("treats date-only / midnight bars as daily (date axis)", () => {
    const daily = [klineRow("2026-06-23"), klineRow("2026-06-24T00:00:00")];
    expect(klineIsIntraday(daily)).toBe(false);
    expect(klineAxisType(daily)).toBe("date");
  });

  it("treats intraday timestamps as minute (category axis, no gaps)", () => {
    const intraday = [klineRow("2026-06-23T09:35:00"), klineRow("2026-06-23 09:40:00")];
    expect(klineIsIntraday(intraday)).toBe(true);
    expect(klineAxisType(intraday)).toBe("category");
  });
});

describe("intraday axis tick labels", () => {
  it("formats time-of-day as 24h HH:MM without date", () => {
    expect(klineTimeLabel("2026-06-23T09:35:00")).toBe("09:35");
    expect(klineTimeLabel("2026-06-23 13:00:00")).toBe("13:00");
  });

  it("returns sparse evenly-spaced ticks including the first and last bar, time-only", () => {
    const rows = Array.from({ length: 48 }, (_, i) => {
      const hh = String(9 + Math.floor(i / 12)).padStart(2, "0");
      const mm = String((i % 12) * 5).padStart(2, "0");
      return klineRow(`2026-06-23T${hh}:${mm}:00`);
    });
    const { tickvals, ticktext } = klineCategoryTicks(rows, undefined, 7);
    expect(tickvals.length).toBeGreaterThan(1);
    expect(tickvals.length).toBeLessThanOrEqual(7);
    expect(tickvals[0]).toBe(0);
    expect(tickvals[tickvals.length - 1]).toBe(47);
    expect(ticktext.every((s) => /^\d{2}:\d{2}$/.test(s))).toBe(true);
  });

  it("restricts ticks to the visible (zoomed) index window", () => {
    const rows = Array.from({ length: 48 }, (_, i) => klineRow(`2026-06-23T10:${String(i).padStart(2, "0")}:00`));
    const { tickvals } = klineCategoryTicks(rows, [23.5, 47.5], 5);
    expect(tickvals[0]).toBe(24);
    expect(tickvals[tickvals.length - 1]).toBe(47);
  });
});

type MockFactor = {
  factor_name: string;
  factor_expression: string;
  categories?: string[];
};

type MockStrategy = {
  strategy_name: string;
  metrics?: Record<string, unknown>;
};

function renderLibraryPage() {
  return render(
    <I18nProvider>
      <ToastProvider>
        <LibraryPage />
      </ToastProvider>
    </I18nProvider>,
  );
}

function renderTimingPage() {
  return render(
    <I18nProvider>
      <ToastProvider>
        <TimingPage />
      </ToastProvider>
    </I18nProvider>,
  );
}

function mockPortalFetch({
  factors = [],
  strategies = [],
}: {
  factors?: MockFactor[];
  strategies?: MockStrategy[];
} = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/factors" && (!init || init.method === undefined)) {
      return Response.json({ factors, categories: [], supports_categories: true });
    }
    if (path === "/api/strategies" && (!init || init.method === undefined)) {
      return Response.json({ strategies, names: strategies.map((strategy) => strategy.strategy_name) });
    }
    if (path === "/api/report-factors/ocr-providers") {
      return Response.json({
        default_provider: "azure",
        modes: ["auto", "local", "azure"],
        providers: [{ provider_id: "azure", display_name: "Azure Document Intelligence", source: "built_in" }],
      });
    }
    if (path === "/api/factors" && init?.method === "POST") {
      return Response.json({
        acceptable: false,
        code: "duplicate_expression",
        message: "An identical factor expression already exists in the zoo.",
        details: { factor_name: "existing_factor" },
      });
    }
    if (path.startsWith("/api/factors/") && init?.method === "DELETE") {
      return Response.json({ deleted: true });
    }
    if (path.startsWith("/api/strategies/") && init?.method === "DELETE") {
      return Response.json({ deleted: true });
    }
    return Response.json({}, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function mockTimingFetch() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/trading/strategy-definitions") {
      return Response.json({
        definitions: [
          {
            strategy_id: "boll_mean_reversion",
            version: "1.0.0",
            signal_kind: "instrument_timing",
            description: "BOLL mean reversion",
            parameter_schema: { properties: { window: { default: 20 }, num_std: { default: 2 } } },
          },
          {
            strategy_id: "dual_ma",
            version: "1.0.0",
            signal_kind: "instrument_timing",
            description: "Dual moving average",
            parameter_schema: { properties: { short_window: { default: 5 }, long_window: { default: 20 } } },
          },
        ],
      });
    }
    if (path === "/api/trading/strategy-instances") {
      return Response.json({ instances: [{
        instance_id: "boll_demo", strategy_id: "boll_mean_reversion",
        validation_state: "validated", config_hash: "abc1234567890",
      }] });
    }
    if (path === "/api/trading/strategy-instances/boll_demo/preview" && init?.method === "POST") {
      return Response.json({
        signal: { as_of: "2026-01-01", payload: { scores: { "000001.SZSE": 1 }, states: { "000001.SZSE": "long" } } },
      });
    }
    if (path === "/api/trading/strategy-instances/boll_demo/backtest-runs" && init?.method === "POST") {
      return Response.json({ run_id: "timing-job", status: "running" });
    }
    if (path === "/api/trading/backtest-runs/timing-job") {
      return Response.json({ run_id: "timing-job", status: "running" });
    }
    if (path === "/api/jobs") {
      return Response.json([]);
    }
    return Response.json({}, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function hasDeleteCall(fetchMock: ReturnType<typeof mockPortalFetch>, path: string) {
  return fetchMock.mock.calls.some(([input, init]) => String(input) === path && init?.method === "DELETE");
}

function postedJson(fetchMock: ReturnType<typeof mockTimingFetch>, path: string) {
  const call = fetchMock.mock.calls.find(([input, init]) => String(input) === path && init?.method === "POST");
  if (!call) return null;
  return JSON.parse(String(call[1]?.body || "{}")) as Record<string, unknown>;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("LibraryPage factor add", () => {
  it("keeps the form and shows an error when the API rejects a duplicate factor", async () => {
    const fetchMock = mockPortalFetch();
    renderLibraryPage();

    const nameInput = await screen.findByPlaceholderText("factor_name");
    const expressionInput = screen.getByPlaceholderText("factor_expression");

    fireEvent.change(nameInput, { target: { value: "new_factor" } });
    fireEvent.change(expressionInput, { target: { value: "$close / $open" } });

    const addPanel = nameInput.closest("aside");
    expect(addPanel).not.toBeNull();
    fireEvent.click(within(addPanel as HTMLElement).getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(screen.getByText("An identical factor expression already exists in the zoo.")).toBeInTheDocument();
    });
    expect(nameInput).toHaveValue("new_factor");
    expect(expressionInput).toHaveValue("$close / $open");
    expect(fetchMock.mock.calls.filter(([path, init]) => String(path) === "/api/factors" && init?.method === "POST")).toHaveLength(1);
  });

  it("reviews extracted PDF factors before explicitly committing them", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/factors" && (!init || init.method === undefined)) {
        return Response.json({ factors: [], categories: [], supports_categories: true });
      }
      if (path === "/api/strategies") return Response.json({ strategies: [], names: [] });
      if (path === "/api/data/instrument-sets") return Response.json({ sets: [] });
      if (path === "/api/report-factors/ocr-providers") {
        return Response.json({
          default_provider: "azure",
          modes: ["auto", "local", "azure", "vendor"],
          providers: [{ provider_id: "vendor", display_name: "Vendor OCR", source: "entry_point" }],
        });
      }
      if (path === "/api/report-factors/extract" && init?.method === "POST") {
        return Response.json({ job_id: "report-job", kind: "report_factor_extract", status: "running" });
      }
      if (path === "/api/jobs/report-job/progress") {
        return Response.json({ job_id: "report-job", status: "succeeded", percent: 100, stage: "done" });
      }
      if (path === "/api/jobs/report-job/result") {
        return Response.json({ result: {
          schema_version: "1.0",
          report: { file_name: "research.pdf", sha256: "abc", page_count: 2, parser: "pypdf", ocr_used: false, classification: { relevant: true, label: "quant_factor_research", reason: "factor study" } },
          summary: "Momentum report",
          warnings: [],
          factors: [{
            draft_id: "draft-1", factor_name: "momentum_5d", description: "five-day momentum",
            formulation: "P_t/P_{t-5}-1", variables: { P: "close" }, factor_expression: "$close/Ref($close,5)-1",
            source_pages: [2], evidence: ["five-day momentum"], viability: { status: "viable", reason: "daily close exists" },
            validation: { acceptable: true, code: "ok", message: "valid" }, warnings: [],
          }],
        } });
      }
      if (path === "/api/factors/validate" && init?.method === "POST") {
        return Response.json({ acceptable: true, code: "ok", message: "valid" });
      }
      if (path === "/api/report-factors/commit" && init?.method === "POST") {
        return Response.json({ n_requested: 1, n_committed: 1, n_rejected: 0, committed: [], rejected: [] });
      }
      return Response.json({}, { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderLibraryPage();

    const ocrMode = await screen.findByLabelText("OCR 模式");
    await screen.findByRole("option", { name: "vendor" });
    fireEvent.change(ocrMode, { target: { value: "vendor" } });
    fireEvent.change(await screen.findByLabelText("服务器 PDF 路径"), { target: { value: "important_data/research.pdf" } });
    fireEvent.click(screen.getByRole("button", { name: "开始提取" }));
    const factorName = await screen.findByDisplayValue("momentum_5d");
    const card = factorName.closest(".dup-group") as HTMLElement;
    fireEvent.click(within(card).getByRole("checkbox"));
    fireEvent.click(within(card).getByRole("button", { name: "校验" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => String(path) === "/api/factors/validate")).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "确认选中项入库" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([path]) => String(path) === "/api/report-factors/commit")).toBe(true));
    const commitCall = fetchMock.mock.calls.find(([path]) => String(path) === "/api/report-factors/commit");
    expect(JSON.parse(String(commitCall?.[1]?.body))).toMatchObject({
      job_id: "report-job",
      factors: [{ draft_id: "draft-1", factor_name: "momentum_5d" }],
    });
    const extractCall = fetchMock.mock.calls.find(([path]) => String(path) === "/api/report-factors/extract");
    expect(JSON.parse(String(extractCall?.[1]?.body))).toMatchObject({ ocr_mode: "vendor" });
  });
});

describe("LibraryPage delete confirmations", () => {
  it("does not delete a factor when the confirmation is cancelled", async () => {
    const fetchMock = mockPortalFetch({
      factors: [{ factor_name: "factor_to_delete", factor_expression: "$close" }],
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderLibraryPage();

    const row = (await screen.findByText("factor_to_delete")).closest("tr");
    expect(row).not.toBeNull();
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "删除" }));

    expect(confirmSpy).toHaveBeenCalledWith("删除 factor_to_delete?");
    expect(hasDeleteCall(fetchMock, "/api/factors/factor_to_delete")).toBe(false);
  });

  it("deletes a factor only after confirmation", async () => {
    const fetchMock = mockPortalFetch({
      factors: [{ factor_name: "confirmed_factor", factor_expression: "$open" }],
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderLibraryPage();

    const row = (await screen.findByText("confirmed_factor")).closest("tr");
    expect(row).not.toBeNull();
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(hasDeleteCall(fetchMock, "/api/factors/confirmed_factor")).toBe(true);
    });
  });

  it("does not delete a strategy when the confirmation is cancelled", async () => {
    const fetchMock = mockPortalFetch({
      strategies: [{ strategy_name: "strategy_to_delete", metrics: {} }],
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    renderLibraryPage();

    fireEvent.click(await screen.findByRole("button", { name: "策略" }));
    const row = (await screen.findByText("strategy_to_delete")).closest("tr");
    expect(row).not.toBeNull();
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "删除" }));

    expect(confirmSpy).toHaveBeenCalledWith("删除 strategy_to_delete?");
    expect(hasDeleteCall(fetchMock, "/api/strategies/strategy_to_delete")).toBe(false);
  });

  it("deletes a strategy only after confirmation", async () => {
    const fetchMock = mockPortalFetch({
      strategies: [{ strategy_name: "confirmed_strategy", metrics: {} }],
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderLibraryPage();

    fireEvent.click(await screen.findByRole("button", { name: "策略" }));
    const row = (await screen.findByText("confirmed_strategy")).closest("tr");
    expect(row).not.toBeNull();
    fireEvent.click(within(row as HTMLElement).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(hasDeleteCall(fetchMock, "/api/strategies/confirmed_strategy")).toBe(true);
    });
  });
});

describe("TimingPage", () => {
  it("previews signals and starts a timing backtest job", async () => {
    const fetchMock = mockTimingFetch();
    renderTimingPage();

    expect(await screen.findByText("BOLL mean reversion")).toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText("运行实例"), { target: { value: "boll_demo" } });

    fireEvent.click(screen.getByRole("button", { name: "预览信号" }));
    await waitFor(() => {
      expect(postedJson(fetchMock, "/api/trading/strategy-instances/boll_demo/preview")).not.toBeNull();
    });
    expect(await screen.findByText("000001.SZSE")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "运行择时回测" }));
    await waitFor(() => {
      expect(postedJson(fetchMock, "/api/trading/strategy-instances/boll_demo/backtest-runs")).not.toBeNull();
    });
    expect(await screen.findByText("timing-job")).toBeInTheDocument();
  });
});
