export function fmtMoney(n: number): string {
  return Number.isFinite(n) ? n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "—";
}

export function compactJson(value: unknown, max = 120): string {
  const text = value === undefined || value === null ? "" : JSON.stringify(value);
  return text.length > max ? `${text.slice(0, max)}...` : text;
}
