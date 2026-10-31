import { describe, expect, it } from "vitest";
import { formatStat, groupLabel, metricLabel } from "./metricLabels";

describe("metric labels", () => {
  it("gives a known metric its label and unit", () => {
    expect(metricLabel("precision")).toEqual({ label: "Precision", unit: "percent" });
    expect(metricLabel("detection_delay_s")).toEqual({
      label: "Detection delay",
      unit: "seconds",
    });
    expect(metricLabel("cost_per_incident").unit).toBe("usd");
    expect(metricLabel("failed_runs").unit).toBe("count");
  });

  it("falls back to the raw name for a metric or group it does not know", () => {
    expect(metricLabel("brand_new_metric")).toEqual({ label: "brand_new_metric", unit: "number" });
    expect(groupLabel("future_group")).toBe("future_group");
    expect(groupLabel("detection")).toBe("Detection");
  });

  it("does not mistake object built-ins for known names", () => {
    expect(metricLabel("constructor")).toEqual({ label: "constructor", unit: "number" });
    expect(metricLabel("toString")).toEqual({ label: "toString", unit: "number" });
    expect(groupLabel("hasOwnProperty")).toBe("hasOwnProperty");
  });

  it("formats each unit", () => {
    expect(formatStat(90, "seconds")).toBe("1 min 30 s");
    expect(formatStat(0.8, "percent")).toBe("80%");
    expect(formatStat(0.125, "percent")).toBe("12.5%");
    expect(formatStat(1.5, "usd")).toBe("$1.50");
    expect(formatStat(1234, "count")).toBe("1,234");
    expect(formatStat(0.5, "ratio")).toBe("0.50");
    expect(formatStat(2.5, "number")).toBe("2.50");
  });
});
