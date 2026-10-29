import { describe, expect, it } from "vitest";
import type { HandoffView } from "@/lib/useHandoff";
import type { MetricPoint, ModelVersionRow } from "@/lib/useTracking";
import {
  buildVersionRows,
  formatMetric,
  metricNames,
  primaryMetric,
  type VersionRow,
} from "./buildVersionRows";

function makeVersion(overrides: Partial<ModelVersionRow> = {}): ModelVersionRow {
  return {
    source_id: "trk_1",
    model: "test_model",
    version: "1",
    created: "2026-10-29T10:00:00Z",
    aliases: ["staging"],
    run_key: "run_alpha",
    evaluation: "present",
    ...overrides,
  };
}

function makeMetric(metric: string, value: number, runKey = "run_alpha"): MetricPoint {
  return {
    source_id: "trk_1",
    run_key: runKey,
    metric,
    value,
    step: 1,
    time: "2026-10-29T10:00:00Z",
  };
}

describe("buildVersionRows", () => {
  it("builds version rows mapping metrics by run_key", () => {
    const versions: ModelVersionRow[] = [
      makeVersion({
        version: "2",
        run_key: "run_beta",
        created: "2026-10-29T11:00:00Z",
        aliases: ["champion"],
      }),
      makeVersion({
        version: "1",
        run_key: "run_alpha",
        created: "2026-10-29T10:00:00Z",
        aliases: [],
      }),
    ];

    const metrics: Record<string, MetricPoint[]> = {
      run_beta: [makeMetric("accuracy", 0.952, "run_beta"), makeMetric("f1", 0.941, "run_beta")],
      run_alpha: [
        makeMetric("accuracy", 0.912, "run_alpha"),
        makeMetric("loss", 0.123, "run_alpha"),
      ],
    };

    const rows = buildVersionRows(versions, metrics);

    expect(rows).toHaveLength(2);
    expect(rows[0]).toEqual({
      version: "2",
      aliases: ["champion"],
      created: "2026-10-29T11:00:00Z",
      runKey: "run_beta",
      metrics: {
        accuracy: 0.952,
        f1: 0.941,
      },
    });
    expect(rows[1]).toEqual({
      version: "1",
      aliases: [],
      created: "2026-10-29T10:00:00Z",
      runKey: "run_alpha",
      metrics: {
        accuracy: 0.912,
        loss: 0.123,
      },
    });
  });

  it("handles a version with no run_key and with missing metrics record", () => {
    const versions: ModelVersionRow[] = [
      makeVersion({ version: "3", run_key: null }),
      makeVersion({ version: "4", run_key: "run_unknown" }),
    ];

    const metrics: Record<string, MetricPoint[]> = {};

    const rows = buildVersionRows(versions, metrics);

    expect(rows).toHaveLength(2);
    expect(rows[0]).toEqual({
      version: "3",
      aliases: ["staging"],
      created: "2026-10-29T10:00:00Z",
      runKey: null,
      metrics: {},
    });
    expect(rows[1]).toEqual({
      version: "4",
      aliases: ["staging"],
      created: "2026-10-29T10:00:00Z",
      runKey: "run_unknown",
      metrics: {},
    });
  });
});

describe("metricNames", () => {
  it("computes the sorted union of all metric names across version rows", () => {
    const rows: VersionRow[] = [
      {
        version: "1",
        aliases: [],
        created: "2026-10-29T10:00:00Z",
        runKey: "run_1",
        metrics: { f1: 0.8, accuracy: 0.9 },
      },
      {
        version: "2",
        aliases: [],
        created: "2026-10-29T11:00:00Z",
        runKey: "run_2",
        metrics: { loss: 0.1, accuracy: 0.95, roc_auc: 0.99 },
      },
    ];

    expect(metricNames(rows)).toEqual(["accuracy", "f1", "loss", "roc_auc"]);
  });

  it("returns empty array when rows have no metrics", () => {
    const rows: VersionRow[] = [
      {
        version: "1",
        aliases: [],
        created: "2026-10-29T10:00:00Z",
        runKey: null,
        metrics: {},
      },
    ];

    expect(metricNames(rows)).toEqual([]);
    expect(metricNames([])).toEqual([]);
  });
});

describe("primaryMetric", () => {
  const handoffStub = {
    version: 1,
    proposal_id: "prop_1",
    approved: "2026-10-29T10:00:00Z",
    payload: {
      models: [
        {
          name: "fraud-detector",
          promotion: "team_gate",
          environments: {},
          labels: null,
          predictions: null,
          production_signal: null,
          commands: [],
          profile: {
            objective: "fraud",
            problem_type: "classification",
            target_column: "is_fraud",
            positive_class: "1",
            primary_metric: "f1_score",
            metric_rationale: "class imbalance",
            cost_ratio: null,
            error_costs: "",
            operating_threshold: 0.5,
            label_delay_days: 0,
            label_maturity_days: 0,
            serving_mode: "online",
            groups: [],
          },
        },
        {
          name: "churn-predictor",
          promotion: "person",
          environments: {},
          labels: null,
          predictions: null,
          production_signal: null,
          commands: [],
          profile: {
            objective: "churn",
            problem_type: "classification",
            target_column: "churn",
            positive_class: "yes",
            primary_metric: "roc_auc",
            metric_rationale: "ranking",
            cost_ratio: null,
            error_costs: "",
            operating_threshold: 0.4,
            label_delay_days: 7,
            label_maturity_days: 14,
            serving_mode: "batch",
            groups: [],
          },
        },
      ],
    },
  } as unknown as HandoffView;

  it("picks the right primary metric when two ModelSpecs exist", () => {
    expect(primaryMetric(handoffStub, "fraud-detector")).toBe("f1_score");
    expect(primaryMetric(handoffStub, "churn-predictor")).toBe("roc_auc");
  });

  it("returns null when handoff is null", () => {
    expect(primaryMetric(null, "fraud-detector")).toBeNull();
  });

  it("returns null when no matching model name is found", () => {
    expect(primaryMetric(handoffStub, "non-existent-model")).toBeNull();
  });
});

describe("formatMetric", () => {
  it("formats numbers to 4 significant digits", () => {
    expect(formatMetric(0.92346)).toBe("0.9235");
    expect(formatMetric(12.3456)).toBe("12.35");
    expect(formatMetric(1234)).toBe("1234");
    expect(formatMetric(0.0012344)).toBe("0.001234");
  });

  it("returns en-dash for undefined, null, or NaN", () => {
    expect(formatMetric(undefined)).toBe("–");
    expect(formatMetric(null)).toBe("–");
    expect(formatMetric(Number.NaN)).toBe("–");
  });
});
