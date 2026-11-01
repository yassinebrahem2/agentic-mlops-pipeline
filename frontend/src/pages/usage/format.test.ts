import { describe, expect, it } from "vitest";
import { formatUsd } from "./format";

describe("formatUsd", () => {
  it("shows a tiny priced cost as below the smallest shown amount, never $0.0000", () => {
    expect(formatUsd(0.00001)).toBe("<$0.0001");
  });

  it("shows zero as $0", () => {
    expect(formatUsd(0)).toBe("$0");
  });

  it("leaves other amounts as they were", () => {
    expect(formatUsd(1.5)).toBe("$1.50");
    expect(formatUsd(0.0001)).toBe("$0.0001");
  });
});
