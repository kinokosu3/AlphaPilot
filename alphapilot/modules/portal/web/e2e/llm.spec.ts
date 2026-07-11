import { expect, test } from "@playwright/test";

test.skip(process.env.ALPHAPILOT_RUN_REAL_LLM !== "1", "Set ALPHAPILOT_RUN_REAL_LLM=1 for the opt-in external smoke test");

test("real LLM mining creates a three-step observable job", async ({ page }) => {
  test.setTimeout(20 * 60_000);
  await page.goto("/mining");
  await page.getByLabel(/迭代步数 Step N/).fill("3");
  await page.getByLabel(/方向 Direction/).fill("量价反转与短期动量的非线性组合");
  const market = page.getByLabel(/股票池 Stock pool/);
  await market.selectOption("qa_stock_pool_30");
  await page.getByRole("button", { name: /运行|Run/ }).first().click();
  await expect(page.getByText(/已启动|Started/).last()).toBeVisible();
  const jobRow = page.locator("table tbody tr", { hasText: "mine" }).first();
  await expect(jobRow).toBeVisible();
  await expect(jobRow).not.toContainText("running", { timeout: 18 * 60_000 });
  await expect(jobRow).toContainText(/succeeded|成功/i);
  await jobRow.getByRole("button", { name: /打开|Open/ }).click();
  await expect(page.locator("pre.log")).toContainText(/factor|因子|expression|表达式/i);
  await expect(page.locator("pre.log")).not.toContainText(/OPENAI_API_KEY|Bearer\s+[A-Za-z0-9_-]{12,}/i);

  const sessions = page.getByRole("heading", { name: /挖掘会话|Mining sessions/i }).locator("xpath=ancestor::section[1]");
  await sessions.getByRole("button", { name: /刷新|Refresh/ }).click();
  const sessionRow = sessions.locator("tbody tr").first();
  await expect(sessionRow).toBeVisible();
  await sessionRow.getByRole("button", { name: /打开|Open/ }).click();
  await expect(sessions.locator("pre.json")).toContainText(/name|path|files/i);
});
