const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

const TOKENS = new Intl.NumberFormat("en-US");

export function formatUsd(amount: number): string {
  return USD.format(amount);
}

export function formatTokens(count: number): string {
  return TOKENS.format(count);
}
