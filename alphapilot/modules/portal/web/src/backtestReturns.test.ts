import { describe, expect, it } from "vitest";
import { navReturnSeries, summarizeBacktest } from "./backtestReturns";

const rows = [
  { date: "2026-01-05", return: 0.10, cost: 0.01, bench: 0.02, turnover: 0.20, account: 1090 },
  { date: "2026-01-06", return: -0.10, cost: 0.00, bench: 0.01, turnover: 0.40, account: 981 }
];

describe("backtest NAV returns", () => {
  it("compounds strategy, net, benchmark, and relative excess returns", () => {
    const result = navReturnSeries(rows);

    expect(result.date).toEqual(["2026-01-05", "2026-01-06"]);
    expect(result.stratNoCost[0]).toBeCloseTo(0.10);
    expect(result.stratNoCost[1]).toBeCloseTo(-0.01);
    expect(result.stratCost[0]).toBeCloseTo(0.09);
    expect(result.stratCost[1]).toBeCloseTo(-0.019);
    expect(result.bench[1]).toBeCloseTo(0.0302);
    expect(result.excessNoCost[1]).toBeCloseTo(0.99 / 1.0302 - 1);
    expect(result.excessCost[1]).toBeCloseTo(0.981 / 1.0302 - 1);
  });

  it("summarizes net NAV return and drawdown instead of arithmetic sums", () => {
    const summary = summarizeBacktest(rows);

    expect(summary.navReturnNet).toBeCloseTo(-0.019);
    expect(summary.benchmarkNavReturn).toBeCloseTo(0.0302);
    expect(summary.excessNavReturnGross).toBeCloseTo(0.99 / 1.0302 - 1);
    expect(summary.maxDrawdownNet).toBeCloseTo(-0.10);
    expect(summary.meanTurnover).toBeCloseTo(0.30);
    expect(summary.totalCost).toBeCloseTo(0.01);
    expect(summary.finalAccount).toBe(981);
  });

  it("returns zeroed summary values for an empty report", () => {
    const summary = summarizeBacktest([]);
    expect(summary.navReturnNet).toBe(0);
    expect(summary.benchmarkNavReturn).toBe(0);
    expect(summary.maxDrawdownNet).toBe(0);
  });

  it("counts a first-day loss as drawdown from the initial NAV", () => {
    const summary = summarizeBacktest([
      { date: "2026-01-05", return: -0.20, cost: 0 },
      { date: "2026-01-06", return: 0.10, cost: 0 }
    ]);
    expect(summary.maxDrawdownNet).toBeCloseTo(-0.20);
  });
});
