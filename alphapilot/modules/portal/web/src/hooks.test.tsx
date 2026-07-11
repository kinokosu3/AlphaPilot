import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useAsync, useSerialPolling } from "./hooks";

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
