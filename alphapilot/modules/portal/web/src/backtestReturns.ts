export type BacktestRow = Record<string, unknown>;

export const numv = (value: unknown): number => {
  const parsed = typeof value === "number" ? value : parseFloat(String(value));
  return Number.isFinite(parsed) ? parsed : 0;
};

export const dayKey = (value: unknown): string => String(value ?? "").slice(0, 10);

// Report rows are named `date` (the API renames the index), with `datetime` kept
// as a fallback for older artifacts.
export const repDay = (row: BacktestRow): string => dayKey(row.date ?? row.datetime);

export type NavReturnSeries = {
  date: string[];
  stratNoCost: number[];
  stratCost: number[];
  bench: number[];
  excessNoCost: number[];
  excessCost: number[];
};

const relativeReturn = (strategyNav: number, benchmarkNav: number): number =>
  benchmarkNav === 0 ? 0 : strategyNav / benchmarkNav - 1;

/** Compound daily returns into return on a 1.0 NAV base. */
export function navReturnSeries(rows: BacktestRow[]): NavReturnSeries {
  const date: string[] = [];
  const stratNoCost: number[] = [];
  const stratCost: number[] = [];
  const bench: number[] = [];
  const excessNoCost: number[] = [];
  const excessCost: number[] = [];
  let grossNav = 1;
  let netNav = 1;
  let benchmarkNav = 1;

  for (const row of rows) {
    const dailyReturn = numv(row["return"]);
    const dailyCost = numv(row.cost);
    const benchmarkReturn = numv(row.bench);
    grossNav *= 1 + dailyReturn;
    netNav *= 1 + dailyReturn - dailyCost;
    benchmarkNav *= 1 + benchmarkReturn;

    date.push(repDay(row));
    stratNoCost.push(grossNav - 1);
    stratCost.push(netNav - 1);
    bench.push(benchmarkNav - 1);
    excessNoCost.push(relativeReturn(grossNav, benchmarkNav));
    excessCost.push(relativeReturn(netNav, benchmarkNav));
  }

  return { date, stratNoCost, stratCost, bench, excessNoCost, excessCost };
}

const lastOrZero = (values: number[]): number => (values.length ? values[values.length - 1] : 0);

/** Summary values shown on the backtest page, using net NAV for the primary return and drawdown. */
export function summarizeBacktest(rows: BacktestRow[]) {
  const navReturns = navReturnSeries(rows);
  let peakNetNav = 1;
  let maxDrawdownNet = 0;
  let turnoverSum = 0;
  let costSum = 0;
  let lastAccount = 0;

  rows.forEach((row, index) => {
    const netNav = 1 + navReturns.stratCost[index];
    peakNetNav = Math.max(peakNetNav, netNav);
    maxDrawdownNet = Math.min(maxDrawdownNet, netNav / peakNetNav - 1);
    turnoverSum += numv(row.turnover);
    costSum += numv(row.cost);
    if (row.account != null) lastAccount = numv(row.account);
  });

  return {
    navReturnNet: lastOrZero(navReturns.stratCost),
    benchmarkNavReturn: lastOrZero(navReturns.bench),
    excessNavReturnGross: lastOrZero(navReturns.excessNoCost),
    maxDrawdownNet,
    meanTurnover: rows.length ? turnoverSum / rows.length : 0,
    totalCost: costSum,
    finalAccount: lastAccount
  };
}
