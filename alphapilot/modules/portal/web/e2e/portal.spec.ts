import { expect, test } from "@playwright/test";

const routes = ["/", "/mining", "/backtest", "/timing", "/library", "/market", "/daily-trade", "/live", "/scheduler", "/notifications", "/advanced"];

test.describe("Portal interaction contract", () => {
  test("all routes render without unhandled errors or API 500", async ({ page }) => {
    const consoleErrors: string[] = [];
    const apiFailures: string[] = [];
    page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
    page.on("response", (response) => {
      if (response.url().includes("/api/") && response.status() >= 500) apiFailures.push(`${response.status()} ${response.url()}`);
    });
    for (const route of routes) {
      await page.goto(route);
      await expect(page.locator(".shell")).toBeVisible();
      await expect(page.locator(".route-skeleton")).toHaveCount(0);
      await expect(page.locator("body")).toContainText("AlphaPilot");
    }
    expect(apiFailures).toEqual([]);
    expect(consoleErrors.filter((message) => !message.includes("favicon.ico"))).toEqual([]);
  });

  test("all routes keep a stable shell through network failure and browser retry", async ({ page }) => {
    for (const route of routes) {
      await page.route("**/api/**", (request) => request.abort("failed"));
      await page.goto(route);
      await expect(page.locator(".shell")).toBeVisible();
      await expect(page.locator("main")).not.toBeEmpty();
      await page.unroute("**/api/**");
      await page.reload();
      await expect(page.locator(".shell")).toBeVisible();
      await expect(page.locator(".route-skeleton")).toHaveCount(0);
    }
  });

  test("backtest artifact opens a real chart", async ({ page }) => {
    await page.goto("/backtest");
    await page.getByRole("button", { name: /打开|Open/ }).first().click();
    await expect(page.locator(".js-plotly-plot").first()).toBeVisible({ timeout: 20_000 });
  });

  test("Paper daemon follows the browser safety workflow", async ({ page }) => {
    const externalRequests: string[] = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (!["127.0.0.1", "localhost"].includes(url.hostname)) externalRequests.push(request.url());
    });
    await page.goto("/live");
    await expect(page.getByRole("tab", { name: /PAPER/ })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByLabel("初始资金")).toBeVisible();
    await page.getByLabel("初始资金").fill("250000");
    await page.getByRole("button", { name: "启动 daemon" }).click();
    await expect(page.locator(".live-workspace-status .pill.running")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("tab", { name: /LIVE/ })).toBeDisabled();
    await page.getByText("更多技术操作").click();
    await page.getByRole("button", { name: "停止 daemon" }).click();
    await page.getByRole("alertdialog").getByRole("button", { name: "确认" }).click();
    await expect(page.getByRole("button", { name: "启动 daemon" })).toBeVisible({ timeout: 15_000 });
    expect(externalRequests).toEqual([]);
  });

  test("@mobile live safety controls and business tabs stay visible", async ({ page }) => {
    await page.goto("/live");
    await expect(page.locator(".live-workspace-status")).toBeVisible();
    await expect(page.locator(".live-status-actions .danger")).toBeVisible();
    await expect(page.locator(".live-business-tabs")).toBeVisible();
    const columns = await page.locator(".live-workbench-grid").evaluate((element) => getComputedStyle(element).gridTemplateColumns.split(" ").length);
    expect(columns).toBe(1);
  });

  test("@cross-browser core navigation and factor form are operable", async ({ page }) => {
    await page.goto("/library");
    await expect(page.getByRole("heading", { name: /因子.*策略库|Factor/ }).first()).toBeVisible();
    await page.goto("/live");
    await expect(page.getByRole("tab", { name: /PAPER/ })).toBeVisible();
  });
});
