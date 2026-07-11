import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ToastProvider, useAction } from "./toast";

function ActionProbe({ action }: { action: () => Promise<void> }) {
  const { busy, run } = useAction();
  return <button disabled={busy} onClick={() => void run(action)}>{busy ? "busy" : "run"}</button>;
}

describe("useAction", () => {
  it("synchronously rejects duplicate clicks until the first action settles", async () => {
    let resolve: (() => void) | undefined;
    const action = vi.fn(() => new Promise<void>((done) => { resolve = done; }));
    render(<ToastProvider><ActionProbe action={action} /></ToastProvider>);
    const button = screen.getByRole("button", { name: "run" });
    await act(async () => {
      button.click();
      button.click();
    });
    expect(action).toHaveBeenCalledTimes(1);
    await act(async () => { resolve?.(); await Promise.resolve(); });
    expect(screen.getByRole("button", { name: "run" })).toBeEnabled();
  });

  it("reports failures without leaving the action busy", async () => {
    const user = userEvent.setup();
    render(<ToastProvider><ActionProbe action={() => Promise.reject(new Error("network down"))} /></ToastProvider>);
    await user.click(screen.getByRole("button", { name: "run" }));
    expect(await screen.findByText("network down")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "run" })).toBeEnabled();
  });
});
