import { describe, expect, it } from "vitest";
import { formatDuration, formatTime, shortCommit } from "./format";

describe("formatDuration", () => {
  it("handles the required table cases: null, 0, 59, 60, 3665, 7260", () => {
    expect(formatDuration(null)).toBe("–");
    expect(formatDuration(undefined)).toBe("–");
    expect(formatDuration(0)).toBe("0 s");
    expect(formatDuration(59)).toBe("59 s");
    expect(formatDuration(60)).toBe("1 min 0 s");
    expect(formatDuration(3665)).toBe("1 h 1 min");
    expect(formatDuration(7260)).toBe("2 h 1 min");
  });

  it("formats examples cited in plan: 12 s, 3 min 4 s, 1 h 5 min", () => {
    expect(formatDuration(12)).toBe("12 s");
    expect(formatDuration(184)).toBe("3 min 4 s"); // 3 * 60 + 4 = 184
    expect(formatDuration(3900)).toBe("1 h 5 min"); // 1 * 3600 + 5 * 60 = 3900
  });

  it("returns dash for invalid or negative numbers", () => {
    expect(formatDuration(Number.NaN)).toBe("–");
    expect(formatDuration(-10)).toBe("–");
  });
});

describe("formatTime", () => {
  it("formats ISO string with toLocaleString", () => {
    const iso = "2026-10-20T10:00:00Z";
    expect(formatTime(iso)).toBe(new Date(iso).toLocaleString());
  });

  it("returns dash for null, undefined, or invalid date", () => {
    expect(formatTime(null)).toBe("–");
    expect(formatTime(undefined)).toBe("–");
    expect(formatTime("not-a-date")).toBe("–");
  });
});

describe("shortCommit", () => {
  it("takes first 7 characters", () => {
    expect(shortCommit("2f5c92c626371702be371e72a85b777889b10313")).toBe("2f5c92c");
    expect(shortCommit("abcdef123456")).toBe("abcdef1");
  });

  it("passes null and undefined through", () => {
    expect(shortCommit(null)).toBeNull();
    const undef = shortCommit(undefined);
    expect(undef).toBeUndefined();
  });

  it("handles strings shorter than 7 characters", () => {
    expect(shortCommit("abc")).toBe("abc");
  });
});
