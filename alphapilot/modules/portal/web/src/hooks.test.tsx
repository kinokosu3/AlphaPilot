import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { useAsync, useLatestRequest, useParamForm, useSerialPolling } from "./hooks";
import type { FieldSpec } from "./paramSpecs";

function PollingProbe({ loader }: { loader: () => Promise<number> }) {
  const state = useSerialPolling(loader, [loader], { enabled: true, intervalMs: 1000 });
  return <div>{state.data ?? "loading"}</div>;
}

function AsyncProbe({ loader }: { loader: () => Promise<number> }) {
  const state = useAsync(loader, [loader]);
  return (
    <div>
      <button type="button" onClick={() => void state.refresh()}>refresh</button>
      <span>{state.data ?? "loading"}</span>
    </div>
  );
}

function LatestProbe({ loaders }: { loaders: Array<() => Promise<string>> }) {
  const latest = useLatestRequest();
  const [value, setValue] = useState("empty");
  const load = async (index: number) => {
    const result = await latest(loaders[index]);
    if (result.current) setValue(result.data || "empty");
  };
  return <div><button onClick={() => void load(0)}>first</button><button onClick={() => void load(1)}>second</button><span>{value}</span></div>;
}

function ParamProbe({ specs }: { specs: FieldSpec[] }) {
  const form = useParamForm(specs);
  return <div><input aria-label="direction" value={String(form.values.direction || "")} onChange={(event) => form.setValue("direction", event.target.value)} /><span>{String(form.values.strategy_name || "")}</span></div>;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("useSerialPolling", () => {
  it("waits for a request to finish before scheduling the next poll", async () => {
    vi.useFakeTimers();
    let resolveFirst: ((value: number) => void) | undefined;
    const loader = vi.fn()
      .mockImplementationOnce(() => new Promise<number>((resolve) => { resolveFirst = resolve; }))
      .mockResolvedValue(2);
    render(<PollingProbe loader={loader} />);

    await act(async () => {});
    expect(loader).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(3000); });
    expect(loader).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveFirst?.(1);
      await Promise.resolve();
    });
    expect(screen.getByText("1")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("starts the new dependency generation even while the old request is unresolved", async () => {
    let resolveOld: ((value: number) => void) | undefined;
    const oldLoader = vi.fn(() => new Promise<number>((resolve) => { resolveOld = resolve; }));
    const newLoader = vi.fn().mockResolvedValue(2);
    const view = render(<PollingProbe loader={oldLoader} />);
    await act(async () => {});
    view.rerender(<PollingProbe loader={newLoader} />);
    await act(async () => {});
    expect(newLoader).toHaveBeenCalledTimes(1);
    expect(screen.getByText("2")).toBeInTheDocument();
    await act(async () => { resolveOld?.(1); await Promise.resolve(); });
    expect(screen.getByText("2")).toBeInTheDocument();
  });
});

describe("useAsync", () => {
  it("does not let an older request overwrite a newer refresh", async () => {
    const resolvers: Array<(value: number) => void> = [];
    const loader = vi.fn(() => new Promise<number>((resolve) => resolvers.push(resolve)));
    render(<AsyncProbe loader={loader} />);

    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "refresh" }));
    expect(loader).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolvers[1](2);
      await Promise.resolve();
    });
    expect(screen.getByText("2")).toBeInTheDocument();

    await act(async () => {
      resolvers[0](1);
      await Promise.resolve();
    });
    expect(screen.getByText("2")).toBeInTheDocument();
  });
});

describe("useLatestRequest", () => {
  it("only allows the latest event-driven response to update the UI", async () => {
    const resolvers: Array<(value: string) => void> = [];
    const loaders = [0, 1].map(() => () => new Promise<string>((resolve) => resolvers.push(resolve)));
    render(<LatestProbe loaders={loaders} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "first" }));
    await user.click(screen.getByRole("button", { name: "second" }));
    await act(async () => { resolvers[1]("new"); await Promise.resolve(); });
    await act(async () => { resolvers[0]("old"); await Promise.resolve(); });
    expect(screen.getByText("new")).toBeInTheDocument();
  });
});

describe("useParamForm dynamic options", () => {
  it("preserves valid typed fields and only replaces a select value that is no longer available", async () => {
    const base: FieldSpec[] = [
      { key: "direction", label: "Direction", type: "text" },
      { key: "strategy_name", label: "Strategy", type: "select", defaultValue: "boll", options: [] },
    ];
    const view = render(<ParamProbe specs={base} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("direction"), "momentum");
    view.rerender(<ParamProbe specs={[
      base[0],
      { ...base[1], options: [{ label: "Dual MA", value: "dual_ma" }] },
    ]} />);
    await vi.waitFor(() => expect(screen.getByText("dual_ma")).toBeInTheDocument());
    expect(screen.getByLabelText("direction")).toHaveValue("momentum");
  });
});
