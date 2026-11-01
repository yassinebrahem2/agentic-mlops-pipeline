const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 4,
});

const TOKENS = new Intl.NumberFormat("en-US");

export function formatUsd(amount: number): string {
  if (amount === 0) return "$0";
  // A priced call can cost less than the 4 decimals shown; "$0.0000" would read as free.
  if (amount < 0.0001) return "<$0.0001";
  return USD.format(amount);
}

export function formatTokens(count: number): string {
  return TOKENS.format(count);
}
