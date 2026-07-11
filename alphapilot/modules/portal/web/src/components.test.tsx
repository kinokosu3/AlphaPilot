import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PanelHelp } from "./components";
import { I18nProvider } from "./i18n";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("PanelHelp", () => {
  it("shifts a help popover into the viewport when its trigger is near the left edge", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains("help-popover")) {
        return { left: -480, right: 40, top: 40, bottom: 240, width: 520, height: 200, x: -480, y: 40, toJSON: () => ({}) };
      }
      if (this.getAttribute("aria-label") === "Help") {
        return { left: 20, right: 40, top: 12, bottom: 32, width: 20, height: 20, x: 20, y: 12, toJSON: () => ({}) };
      }
      return { left: 0, right: 0, top: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0, toJSON: () => ({}) };
    });
    vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(800);

    render(
      <I18nProvider>
        <PanelHelp label="Help" title="Parameters" items={["First item"]} />
      </I18nProvider>
    );

    fireEvent.click(screen.getByRole("button", { name: "Help" }));

    expect(screen.getByRole("dialog", { name: "Help" })).toHaveStyle({ transform: "translateX(496px)" });
  });
});
