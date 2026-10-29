import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import type { EventRecord } from "@/lib/useProjectEvents";
import type {
  DeploymentRow,
  MetricPoint,
  ModelSummary,
  ModelVersionRow,
  Page_DeploymentRow_,
  Page_MetricPoint_,
  Page_ModelSummary_,
  Page_ModelVersionRow_,
  Page_RunRow_,
  RunRow,
  TrackingSourceView,
} from "@/lib/useTracking";
import type { HandoffView } from "@/lib/useHandoff";
import { project, stubProjects } from "@/test/projects";
import { renderPage } from "@/test/render";
import { TestFakeStream } from "@/pages/inbox/testing";
import { route } from "./route";

const projectStub = {
  id: "prj_1",
  name: "Test Project",
  status: "ok",
  source: "local",
  path: "/test",
};

function makeSource(overrides: Partial<TrackingSourceView> = {}): TrackingSourceView {
  return {
    id: "trk_1",
    type: "mlflow",
    auth: "none",
    credential_set: false,
    last_sync: "2026-10-29T10:00:00Z",
    last_error: null,
    settings: {},
    ...overrides,
  };
}

function makeModel(overrides: Partial<ModelSummary> = {}): ModelSummary {
  return {
    source_id: "trk_1",
    name: "fraud-detector",
    production_version: "2",
    latest_version: "2",
    versions: 2,
    ...overrides,
  };
}

function makeVersion(overrides: Partial<ModelVersionRow> = {}): ModelVersionRow {
  return {
    source_id: "trk_1",
    model: "fraud-detector",
    version: "1",
    created: "2026-10-29T10:00:00Z",
    aliases: ["staging"],
    run_key: "run_v1",
    evaluation: "present",
    ...overrides,
  };
}

function makeMetric(metric: string, value: number, runKey: string): MetricPoint {
  return {
    source_id: "trk_1",
    run_key: runKey,
    metric,
    value,
    step: 1,
    time: "2026-10-29T10:00:00Z",
  };
}

function makeRun(overrides: Partial<RunRow> = {}): RunRow {
  return {
    key: "run_v1",
    source_id: "trk_1",
    experiment: "fraud-experiment-1",
    status: "succeeded",
    start: "2026-10-29T10:00:00Z",
    end: "2026-10-29T10:05:00Z",
    git_commit: "abcdef1234567890",
    datasets: ["fraud_train.csv", "fraud_val.csv"],
    params: { lr: "0.001", optimizer: "adam" },
    tags: { env: "prod" },
    ...overrides,
  };
}

function makeDeployment(overrides: Partial<DeploymentRow> = {}): DeploymentRow {
  return {
    source_id: "trk_1",
    model: "fraud-detector",
    alias: "champion",
    from_version: "1",
    to_version: "2",
    seen_at: "2026-10-29T11:00:00Z",
    ...overrides,
  };
}

function getQuery(init: unknown) {
  if (!init || typeof init !== "object") return undefined;
  if (!("params" in init)) return undefined;
  const params = (init as { params: { query?: { model?: string; run_key?: string } } }).params;
  return params.query;
}

const mockHandoff = {
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
          metric_rationale: "imbalance",
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
          target_column: "churned",
          positive_class: "yes",
          primary_metric: "roc_auc",
          metric_rationale: "ranking",
          cost_ratio: null,
          error_costs: "",
          operating_threshold: 0.5,
          label_delay_days: 0,
          label_maturity_days: 0,
          serving_mode: "batch",
          groups: [],
        },
      },
    ],
  },
} as unknown as HandoffView;

describe("TrackedModelsPage", () => {
  let fakeStream: TestFakeStream;
  let getSpy: MockInstance | undefined;
  let postSpy: MockInstance | undefined;
  let openStreamSpy: MockInstance | undefined;

  beforeEach(() => {
    fakeStream = new TestFakeStream();
    openStreamSpy = vi.spyOn(streamModule, "openStream").mockImplementation(() => {
      setTimeout(() => {
        fakeStream.emitOpen();
      }, 0);
      return fakeStream;
    });
    stubProjects([project("prj_1", "Test Project", "2026-10-20T10:00:00Z")]);
  });

  afterEach(() => {
    getSpy?.mockRestore();
    postSpy?.mockRestore();
    openStreamSpy?.mockRestore();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("exports a valid route definition with order 20 and label Models", () => {
    expect(route.path).toBe("/projects/:projectId/models");
    expect(route.nav?.area).toBe("project");
    expect(route.nav?.label).toBe("Models");
    expect(route.nav?.order).toBe(20);
  });

  it("renders null without a projectId", () => {
    const { container } = renderPage("/projects");
    expect(container.querySelector("main")?.textContent).not.toContain("Models");
  });

  it("renders two models, each with versions side by side and the production badge from production_version; a metric absent shows '–'; primary-metric column badged per model", async () => {
    const sources = [makeSource({ id: "trk_1" })];
    const modelsPage: Page_ModelSummary_ = {
      rows: [
        makeModel({
          name: "fraud-detector",
          production_version: "2",
          versions: 2,
        }),
        makeModel({
          name: "churn-predictor",
          production_version: null,
          versions: 1,
        }),
      ],
      total: 2,
      truncated: false,
    };

    const fraudVersionsPage: Page_ModelVersionRow_ = {
      rows: [
        makeVersion({
          model: "fraud-detector",
          version: "2",
          run_key: "run_v2",
          created: "2026-10-29T11:00:00Z",
          aliases: ["production"],
        }),
        makeVersion({
          model: "fraud-detector",
          version: "1",
          run_key: "run_v1",
          created: "2026-10-29T10:00:00Z",
          aliases: ["staging"],
        }),
      ],
      total: 2,
      truncated: false,
    };

    const churnVersionsPage: Page_ModelVersionRow_ = {
      rows: [
        makeVersion({
          model: "churn-predictor",
          version: "1",
          run_key: "run_churn_1",
          created: "2026-10-29T09:00:00Z",
          aliases: ["latest"],
        }),
      ],
      total: 1,
      truncated: false,
    };

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/sources") {
        return Promise.resolve({ data: sources, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({
          data: mockHandoff,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/models") {
        return Promise.resolve({ data: modelsPage, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/tracking/versions") {
        const query = getQuery(init);
        if (query?.model === "churn-predictor") {
          return Promise.resolve({
            data: churnVersionsPage,
            response: new Response(null, { status: 200 }),
          });
        }
        return Promise.resolve({
          data: fraudVersionsPage,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/metrics") {
        const query = getQuery(init);
        if (query?.run_key === "run_v2") {
          const res: Page_MetricPoint_ = {
            rows: [
              makeMetric("f1_score", 0.9412, "run_v2"),
              makeMetric("accuracy", 0.9654, "run_v2"),
            ],
            total: 2,
            truncated: false,
          };
          return Promise.resolve({ data: res, response: new Response(null, { status: 200 }) });
        }
        if (query?.run_key === "run_v1") {
          // Absent accuracy
          const res: Page_MetricPoint_ = {
            rows: [makeMetric("f1_score", 0.8951, "run_v1")],
            total: 1,
            truncated: false,
          };
          return Promise.resolve({ data: res, response: new Response(null, { status: 200 }) });
        }
        if (query?.run_key === "run_churn_1") {
          const res: Page_MetricPoint_ = {
            rows: [makeMetric("roc_auc", 0.8876, "run_churn_1")],
            total: 1,
            truncated: false,
          };
          return Promise.resolve({ data: res, response: new Response(null, { status: 200 }) });
        }
      }
      if (path === "/api/projects/{project_id}/tracking/deployments") {
        const emptyDeploys: Page_DeploymentRow_ = { rows: [], total: 0, truncated: false };
        return Promise.resolve({
          data: emptyDeploys,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/models");

    // Header rendered
    expect(await screen.findByRole("heading", { name: "Models" })).toBeInTheDocument();

    // Two model cards rendered
    expect(await screen.findByText("fraud-detector")).toBeInTheDocument();
    expect(await screen.findByText("churn-predictor")).toBeInTheDocument();

    // Production badge: fraud-detector has production_version "2", churn-predictor has null
    expect(await screen.findByText("Production: 2")).toBeInTheDocument();
    // Ensure no production badge for churn-predictor
    expect(screen.queryByText(/Production: null/)).not.toBeInTheDocument();

    // Metrics formatted: 4 sig digits
    expect(await screen.findByText("0.9412")).toBeInTheDocument();
    expect(await screen.findByText("0.9654")).toBeInTheDocument();
    expect(await screen.findByText("0.8951")).toBeInTheDocument();
    // Absent metric cell shows "–"
    const dashes = await screen.findAllByText("–");
    expect(dashes.length).toBeGreaterThan(0);

    // Primary badges: fraud-detector has primary metric f1_score; churn-predictor has roc_auc
    const primaryBadges = await screen.findAllByText("primary");
    expect(primaryBadges).toHaveLength(2);
  });

  it("expanding a version shows run, short commit, datasets ('Not recorded' when empty), 'Run not tracked' for missing run, and 'removed' in alias history", async () => {
    const user = userEvent.setup();
    const sources = [makeSource({ id: "trk_1" })];
    const modelsPage: Page_ModelSummary_ = {
      rows: [makeModel({ name: "fraud-detector", versions: 2 })],
      total: 1,
      truncated: false,
    };

    const versionsPage: Page_ModelVersionRow_ = {
      rows: [
        makeVersion({
          model: "fraud-detector",
          version: "3",
          run_key: "run_empty_lookup",
        }),
        makeVersion({
          model: "fraud-detector",
          version: "2",
          run_key: "run_tracked",
        }),
        makeVersion({
          model: "fraud-detector",
          version: "1",
          run_key: null, // null run_key
        }),
      ],
      total: 3,
      truncated: false,
    };

    const runDetails = makeRun({
      key: "run_tracked",
      experiment: "deep-fraud-v2",
      git_commit: "abcdef1234567890",
      datasets: ["transactions.csv"],
    });

    const deploymentsPage: Page_DeploymentRow_ = {
      rows: [
        makeDeployment({
          alias: "staging",
          from_version: "1",
          to_version: null, // removed
        }),
      ],
      total: 1,
      truncated: false,
    };

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/sources") {
        return Promise.resolve({ data: sources, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({
          data: mockHandoff,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/models") {
        return Promise.resolve({ data: modelsPage, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/tracking/versions") {
        return Promise.resolve({
          data: versionsPage,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/metrics") {
        const res: Page_MetricPoint_ = { rows: [], total: 0, truncated: false };
        return Promise.resolve({ data: res, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/tracking/runs") {
        const query = getQuery(init);
        if (query?.run_key === "run_tracked") {
          const res: Page_RunRow_ = { rows: [runDetails], total: 1, truncated: false };
          return Promise.resolve({ data: res, response: new Response(null, { status: 200 }) });
        }
        const res: Page_RunRow_ = { rows: [], total: 0, truncated: false };
        return Promise.resolve({ data: res, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/tracking/deployments") {
        return Promise.resolve({
          data: deploymentsPage,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/models");

    expect(await screen.findByText("fraud-detector")).toBeInTheDocument();

    // Expand version 2 (with tracked run)
    const version2Button = await screen.findByRole("button", { name: "2" });
    await user.click(version2Button);

    // Lineage details shown
    expect(await screen.findByText("deep-fraud-v2")).toBeInTheDocument();
    expect(await screen.findByText("Succeeded")).toBeInTheDocument();
    // Short commit in text, full SHA in title
    const commitEl = await screen.findByTitle("abcdef1234567890");
    expect(commitEl).toHaveTextContent("abcdef1");
    // Training data
    expect(await screen.findByText("transactions.csv")).toBeInTheDocument();
    // Link to runs page
    const runsLink = screen.getByRole("link", { name: "Runs page" });
    expect(runsLink).toHaveAttribute("href", "/projects/prj_1/runs");

    // Expand version 3 (with run_key that returns empty /runs?run_key=)
    const version3Button = await screen.findByRole("button", { name: "3" });
    await user.click(version3Button);
    expect(await screen.findByText("Run not tracked")).toBeInTheDocument();

    // Expand version 1 (with null run_key)
    const version1Button = await screen.findByRole("button", { name: "1" });
    await user.click(version1Button);
    const untrackedList = await screen.findAllByText("Run not tracked");
    expect(untrackedList.length).toBeGreaterThanOrEqual(2);

    // Expand alias history collapsible
    const aliasHistoryTrigger = await screen.findByRole("button", { name: /Alias history/i });
    await user.click(aliasHistoryTrigger);

    // A null to_version in alias history shows "removed"
    expect(await screen.findByText(/removed/)).toBeInTheDocument();
  });

  it("shows 'Showing the latest N of TOTAL versions' and 'History truncated' when pages are truncated", async () => {
    const user = userEvent.setup();
    const sources = [makeSource({ id: "trk_1" })];
    const modelsPage: Page_ModelSummary_ = {
      rows: [makeModel({ name: "big-model", versions: 150 })],
      total: 1,
      truncated: false,
    };

    const versionsPage: Page_ModelVersionRow_ = {
      rows: [makeVersion({ model: "big-model", version: "150", run_key: null })],
      total: 150,
      truncated: true,
    };

    const deploymentsPage: Page_DeploymentRow_ = {
      rows: [makeDeployment({ model: "big-model" })],
      total: 25,
      truncated: true,
    };

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/sources") {
        return Promise.resolve({ data: sources, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
      }
      if (path === "/api/projects/{project_id}/tracking/models") {
        return Promise.resolve({ data: modelsPage, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/tracking/versions") {
        return Promise.resolve({
          data: versionsPage,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/metrics") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/deployments") {
        return Promise.resolve({
          data: deploymentsPage,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/models");

    expect(await screen.findByText("Showing the latest 1 of 150 versions")).toBeInTheDocument();

    const aliasHistoryTrigger = await screen.findByRole("button", { name: /Alias history/i });
    await user.click(aliasHistoryTrigger);

    expect(await screen.findByText("History truncated")).toBeInTheDocument();
  });

  it("handles empty states (no sources, sources but no models) and error states", async () => {
    // 1. No sources
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/sources") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    const { unmount } = renderPage("/projects/prj_1/models");

    expect(
      await screen.findByText(
        "No tracking source yet. Onboarding creates them when you approve the handoff.",
      ),
    ).toBeInTheDocument();

    unmount();
    getSpy.mockRestore();

    // 2. Sources but no models
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/sources") {
        return Promise.resolve({
          data: [makeSource()],
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/models") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    const view2 = renderPage("/projects/prj_1/models");
    expect(await screen.findByText("No models tracked yet")).toBeInTheDocument();

    view2.unmount();
    getSpy.mockRestore();

    // 3. Source last_error in a warning alert at the top
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/sources") {
        return Promise.resolve({
          data: [makeSource({ last_error: "MLflow connection timeout" })],
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/models") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    const view3 = renderPage("/projects/prj_1/models");
    expect(await screen.findByText(/MLflow connection timeout/)).toBeInTheDocument();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("MLflow connection timeout");

    view3.unmount();
  });

  it("tracking.synced event refetches sources and models", async () => {
    let sourcesCalls = 0;
    let modelsCalls = 0;

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/sources") {
        sourcesCalls++;
        return Promise.resolve({
          data: [makeSource()],
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/models") {
        modelsCalls++;
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/handoff") {
        return Promise.resolve({ data: null, response: new Response(null, { status: 404 }) });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/models");

    expect(await screen.findByText("No models tracked yet")).toBeInTheDocument();
    expect(sourcesCalls).toBeGreaterThanOrEqual(1);
    expect(modelsCalls).toBeGreaterThanOrEqual(1);

    // Emit tracking.synced event
    act(() => {
      fakeStream.emitOpen();
      const mockEvent: EventRecord = {
        id: "ev_sync_1",
        seq: 1,
        ts: "2026-10-29T12:00:00Z",
        source: "orchestrator",
        type: "tracking.synced",
        subject_id: "trk_1",
        payload: {},
      };
      fakeStream.emit("tracking.synced", mockEvent, 1);
    });

    await waitFor(() => {
      expect(sourcesCalls).toBeGreaterThanOrEqual(2);
      expect(modelsCalls).toBeGreaterThanOrEqual(2);
    });
  });
});
