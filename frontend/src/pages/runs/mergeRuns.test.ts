import { describe, expect, it } from "vitest";
import type { Page_CiRunRow_, Page_RunRow_, RunRow, CiRunRow } from "@/lib/useTracking";
import { mergeRuns } from "./mergeRuns";

function makeRun(overrides: Partial<RunRow> = {}): RunRow {
  return {
    key: "run_1",
    source_id: "trk_1",
    experiment: "experiment-alpha",
    status: "succeeded",
    start: "2026-10-29T10:00:00Z",
    end: "2026-10-29T10:02:30Z",
    git_commit: "abcdef1234567890",
    datasets: ["mnist.csv"],
    params: { lr: "0.01" },
    tags: {},
    ...overrides,
  };
}

function makeCi(overrides: Partial<CiRunRow> = {}): CiRunRow {
  return {
    key: "ci_1",
    source_id: "trk_2",
    workflow: "build-and-test",
    status: "succeeded",
    start: "2026-10-29T09:30:00Z",
    duration_s: 180,
    commit: "fedcba0987654321",
    url: "https://github.com/org/repo/actions/runs/123",
    ...overrides,
  };
}

describe("mergeRuns", () => {
  it("orders runs newest first by start with null starts last", () => {
    const runsPage: Page_RunRow_ = {
      rows: [
        makeRun({ key: "r1", start: "2026-10-29T08:00:00Z" }),
        makeRun({ key: "r2", start: null }),
        makeRun({ key: "r3", start: "2026-10-29T11:00:00Z" }),
      ],
      total: 3,
      truncated: false,
    };
    const ciPage: Page_CiRunRow_ = {
      rows: [
        makeCi({ key: "c1", start: "2026-10-29T12:00:00Z" }),
        makeCi({ key: "c2", start: "2026-10-29T09:00:00Z" }),
      ],
      total: 2,
      truncated: false,
    };

    const merged = mergeRuns(runsPage, ciPage);

    expect(merged.map((m) => m.key)).toEqual(["c1", "r3", "c2", "r1", "r2"]);
    expect(merged[4]?.start).toBeNull();
  });

  it("calculates durations: end - start for run (null while running), duration_s for ci", () => {
    const runsPage: Page_RunRow_ = {
      rows: [
        makeRun({
          key: "r_succeeded",
          status: "succeeded",
          start: "2026-10-29T10:00:00Z",
          end: "2026-10-29T10:02:30Z", // 150s
        }),
        makeRun({
          key: "r_running",
          status: "running",
          start: "2026-10-29T10:05:00Z",
          end: "2026-10-29T10:10:00Z", // running -> null duration
        }),
        makeRun({
          key: "r_no_end",
          status: "failed",
          start: "2026-10-29T10:00:00Z",
          end: null, // null duration
        }),
      ],
      total: 3,
      truncated: false,
    };
    const ciPage: Page_CiRunRow_ = {
      rows: [
        makeCi({
          key: "c_timed",
          duration_s: 42,
        }),
        makeCi({
          key: "c_null_duration",
          duration_s: null,
        }),
      ],
      total: 2,
      truncated: false,
    };

    const merged = mergeRuns(runsPage, ciPage);
    const byKey = Object.fromEntries(merged.map((m) => [m.key, m]));

    expect(byKey["r_succeeded"]?.durationSeconds).toBe(150);
    expect(byKey["r_running"]?.durationSeconds).toBeNull();
    expect(byKey["r_no_end"]?.durationSeconds).toBeNull();
    expect(byKey["c_timed"]?.durationSeconds).toBe(42);
    expect(byKey["c_null_duration"]?.durationSeconds).toBeNull();
  });

  it("formats details and urls correctly for both kinds", () => {
    const runsPage: Page_RunRow_ = {
      rows: [
        makeRun({
          key: "r1",
          git_commit: "abcdef123456",
          datasets: ["train.csv", "val.csv"],
        }),
      ],
      total: 1,
      truncated: false,
    };
    const ciPage: Page_CiRunRow_ = {
      rows: [
        makeCi({
          key: "c1",
          commit: "fedcba987654",
          url: "https://ci.example.com/1",
        }),
      ],
      total: 1,
      truncated: false,
    };

    const merged = mergeRuns(runsPage, ciPage);
    const r1 = merged.find((m) => m.key === "r1");
    const c1 = merged.find((m) => m.key === "c1");

    expect(r1?.kind).toBe("run");
    expect(r1?.detail).toContain("abcdef1");
    expect(r1?.detail).toContain("train.csv, val.csv");
    expect(r1?.url).toBeNull();

    expect(c1?.kind).toBe("ci");
    expect(c1?.detail).toBe("fedcba9");
    expect(c1?.url).toBe("https://ci.example.com/1");
  });

  describe("truncation cut at the later oldest start", () => {
    it("does not cut any rows when neither page is truncated", () => {
      const runsPage: Page_RunRow_ = {
        rows: [
          makeRun({ key: "r1", start: "2026-10-29T10:00:00Z" }),
          makeRun({ key: "r2", start: "2026-10-29T08:00:00Z" }),
        ],
        total: 2,
        truncated: false,
      };
      const ciPage: Page_CiRunRow_ = {
        rows: [
          makeCi({ key: "c1", start: "2026-10-29T09:00:00Z" }),
          makeCi({ key: "c2", start: "2026-10-29T06:00:00Z" }),
        ],
        total: 2,
        truncated: false,
      };

      const merged = mergeRuns(runsPage, ciPage);
      expect(merged.map((m) => m.key)).toEqual(["r1", "c1", "r2", "c2"]);
    });

    it("cuts at the later oldest start when runs page is truncated", () => {
      // Runs oldest: 08:00 (truncated = true)
      // CI oldest: 06:00 (truncated = false)
      // Later oldest is 08:00. Anything before 08:00 (c2 at 06:00) should be cut out.
      const runsPage: Page_RunRow_ = {
        rows: [
          makeRun({ key: "r1", start: "2026-10-29T10:00:00Z" }),
          makeRun({ key: "r2", start: "2026-10-29T08:00:00Z" }),
        ],
        total: 10,
        truncated: true,
      };
      const ciPage: Page_CiRunRow_ = {
        rows: [
          makeCi({ key: "c1", start: "2026-10-29T09:00:00Z" }),
          makeCi({ key: "c2", start: "2026-10-29T06:00:00Z" }),
        ],
        total: 2,
        truncated: false,
      };

      const merged = mergeRuns(runsPage, ciPage);
      expect(merged.map((m) => m.key)).toEqual(["r1", "c1", "r2"]);
    });

    it("cuts at the later oldest start when CI page is truncated", () => {
      // Runs oldest: 06:00 (truncated = false)
      // CI oldest: 08:00 (truncated = true)
      // Later oldest is 08:00. Anything before 08:00 (r2 at 06:00) should be cut out.
      const runsPage: Page_RunRow_ = {
        rows: [
          makeRun({ key: "r1", start: "2026-10-29T10:00:00Z" }),
          makeRun({ key: "r2", start: "2026-10-29T06:00:00Z" }),
        ],
        total: 2,
        truncated: false,
      };
      const ciPage: Page_CiRunRow_ = {
        rows: [
          makeCi({ key: "c1", start: "2026-10-29T09:00:00Z" }),
          makeCi({ key: "c2", start: "2026-10-29T08:00:00Z" }),
        ],
        total: 10,
        truncated: true,
      };

      const merged = mergeRuns(runsPage, ciPage);
      expect(merged.map((m) => m.key)).toEqual(["r1", "c1", "c2"]);
    });

    it("cuts at the later oldest start when both pages are truncated", () => {
      // Runs oldest: 08:30 (truncated = true)
      // CI oldest: 08:00 (truncated = true)
      // Later oldest is 08:30.
      const runsPage: Page_RunRow_ = {
        rows: [
          makeRun({ key: "r1", start: "2026-10-29T10:00:00Z" }),
          makeRun({ key: "r2", start: "2026-10-29T08:30:00Z" }),
        ],
        total: 20,
        truncated: true,
      };
      const ciPage: Page_CiRunRow_ = {
        rows: [
          makeCi({ key: "c1", start: "2026-10-29T09:00:00Z" }),
          makeCi({ key: "c2", start: "2026-10-29T08:00:00Z" }),
        ],
        total: 20,
        truncated: true,
      };

      const merged = mergeRuns(runsPage, ciPage);
      expect(merged.map((m) => m.key)).toEqual(["r1", "c1", "r2"]);
    });
  });
});
