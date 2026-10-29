import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import type { EventRecord } from "@/lib/useProjectEvents";
import type {
  CiRunRow,
  Page_CiRunRow_,
  Page_RunRow_,
  RunRow,
  TrackingSourceView,
} from "@/lib/useTracking";
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

function makeRun(overrides: Partial<RunRow> = {}): RunRow {
  return {
    key: "run_alpha",
    source_id: "trk_1",
    experiment: "experiment-mnist",
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
    key: "ci_alpha",
    source_id: "trk_2",
    workflow: "build-and-test",
    status: "succeeded",
    start: "2026-10-29T10:05:00Z",
    duration_s: 75,
    commit: "fedcba0987654321",
    url: "https://github.com/org/repo/actions/runs/999",
    ...overrides,
  };
}

describe("RunsPage", () => {
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

  it("exports a valid route definition with order 40 and label Runs", () => {
    expect(route.path).toBe("/projects/:projectId/runs");
    expect(route.nav?.area).toBe("project");
    expect(route.nav?.label).toBe("Runs");
    expect(route.nav?.order).toBe(40);
  });

  it("merges tracked runs and CI runs into one list, newest first, with badges, duration and detail links", async () => {
    const sources: TrackingSourceView[] = [
      makeSource({ id: "trk_1", type: "mlflow", last_sync: "2026-10-29T10:00:00Z" }),
      makeSource({ id: "trk_2", type: "github_actions", last_sync: "2026-10-29T10:10:00Z" }),
    ];
    const runsPage: Page_RunRow_ = {
      rows: [
        makeRun({
          key: "run_finished",
          experiment: "train-bert",
          status: "succeeded",
          start: "2026-10-29T09:00:00Z",
          end: "2026-10-29T09:02:00Z", // 120s -> "2 min 0 s"
          git_commit: "1234567890ab",
          datasets: ["raw.csv"],
        }),
        makeRun({
          key: "run_running",
          experiment: "eval-bert",
          status: "running",
          start: "2026-10-29T11:00:00Z",
          end: null, // running -> "–"
          git_commit: "deadbeefcafe",
          datasets: [],
        }),
      ],
      total: 2,
      truncated: false,
    };
    const ciPage: Page_CiRunRow_ = {
      rows: [
        makeCi({
          key: "ci_run_1",
          workflow: "ci-pipeline",
          status: "failed",
          start: "2026-10-29T10:00:00Z",
          duration_s: 45, // "45 s"
          commit: "abcdef112233",
          url: "https://ci.example.com/build/1",
        }),
      ],
      total: 1,
      truncated: false,
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
      if (path === "/api/projects/{project_id}/tracking/runs") {
        return Promise.resolve({ data: runsPage, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/tracking/ci-runs") {
        return Promise.resolve({ data: ciPage, response: new Response(null, { status: 200 }) });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/runs");

    // Header and sources
    expect(await screen.findByRole("heading", { name: "Runs" })).toBeInTheDocument();
    expect(await screen.findByText("MLflow")).toBeInTheDocument();
    expect(await screen.findByText("GitHub Actions")).toBeInTheDocument();

    // Table rows: sorted newest first -> run_running (11:00), ci_run_1 (10:00), run_finished (09:00)
    const rows = await screen.findAllByRole("row");
    // Row 0 is header, rows 1..3 are data
    expect(rows).toHaveLength(4);

    const r1 = rows[1];
    expect(r1).toBeDefined();
    if (!r1) return;
    const row1 = within(r1);
    expect(row1.getByText("eval-bert")).toBeInTheDocument();
    expect(row1.getByText("Running")).toBeInTheDocument();
    expect(row1.getByText("–")).toBeInTheDocument(); // running duration is "–"

    const r2 = rows[2];
    expect(r2).toBeDefined();
    if (!r2) return;
    const row2 = within(r2);
    expect(row2.getByText("ci-pipeline")).toBeInTheDocument();
    expect(row2.getByText("Failed")).toBeInTheDocument();
    expect(row2.getByText("45 s")).toBeInTheDocument();
    const link = row2.getByRole("link", { name: /abcdef1/ });
    expect(link).toHaveAttribute("href", "https://ci.example.com/build/1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");

    const r3 = rows[3];
    expect(r3).toBeDefined();
    if (!r3) return;
    const row3 = within(r3);
    expect(row3.getByText("train-bert")).toBeInTheDocument();
    expect(row3.getByText("Succeeded")).toBeInTheDocument();
    expect(row3.getByText("2 min 0 s")).toBeInTheDocument();
    expect(row3.getByText(/1234567/)).toBeInTheDocument();
    expect(row3.getByText(/raw\.csv/)).toBeInTheDocument();
  });

  it("filters to only failed rows of both kinds when the Failed tab is selected", async () => {
    const user = userEvent.setup();
    const sources = [makeSource()];
    const runsPage: Page_RunRow_ = {
      rows: [
        makeRun({ key: "r_ok", experiment: "train-success", status: "succeeded" }),
        makeRun({ key: "r_fail", experiment: "train-failed", status: "failed" }),
      ],
      total: 2,
      truncated: false,
    };
    const ciPage: Page_CiRunRow_ = {
      rows: [
        makeCi({ key: "c_ok", workflow: "ci-success", status: "succeeded" }),
        makeCi({ key: "c_fail", workflow: "ci-failed", status: "failed" }),
      ],
      total: 2,
      truncated: false,
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
      if (path === "/api/projects/{project_id}/tracking/runs") {
        return Promise.resolve({ data: runsPage, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/tracking/ci-runs") {
        return Promise.resolve({ data: ciPage, response: new Response(null, { status: 200 }) });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/runs");

    expect(await screen.findByText("train-success")).toBeInTheDocument();
    expect(screen.getByText("train-failed")).toBeInTheDocument();
    expect(screen.getByText("ci-success")).toBeInTheDocument();
    expect(screen.getByText("ci-failed")).toBeInTheDocument();

    // Click "Failed" tab
    const failedTab = screen.getByRole("tab", { name: "Failed" });
    await user.click(failedTab);

    // Only failed rows remain
    expect(screen.queryByText("train-success")).not.toBeInTheDocument();
    expect(screen.queryByText("ci-success")).not.toBeInTheDocument();
    expect(screen.getByText("train-failed")).toBeInTheDocument();
    expect(screen.getByText("ci-failed")).toBeInTheDocument();

    // Switch back to "All"
    const allTab = screen.getByRole("tab", { name: "All" });
    await user.click(allTab);
    expect(screen.getByText("train-success")).toBeInTheDocument();
  });

  it("Sync now posts once without source_id, is disabled while pending, shows server error, and tracking.synced refetches", async () => {
    const user = userEvent.setup();
    let sourcesCallCount = 0;
    let runsCallCount = 0;
    let postCallCount = 0;
    let postBodyCaptured: unknown = "not-called";

    let rejectPost: (err: Error) => void = () => {};

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/sources") {
        sourcesCallCount++;
        return Promise.resolve({
          data: [makeSource({ id: "trk_1" })],
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/runs") {
        runsCallCount++;
        return Promise.resolve({
          data: {
            rows: [makeRun({ key: `r_${String(sourcesCallCount)}` })],
            total: 1,
            truncated: false,
          },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/ci-runs") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    postSpy = vi.spyOn(api, "POST").mockImplementation((path: string, init?: unknown) => {
      if (path === "/api/projects/{project_id}/tracking/sync") {
        postCallCount++;
        postBodyCaptured = (init as { body?: unknown } | undefined)?.body;
        return new Promise((_resolve, reject) => {
          rejectPost = (err: Error) => {
            reject(err);
          };
        });
      }
      return Promise.reject(new Error(`Unexpected POST ${path}`));
    });

    renderPage("/projects/prj_1/runs");

    const syncButton = await screen.findByRole("button", { name: /Sync now/i });
    expect(syncButton).toBeEnabled();

    // Trigger sync
    await user.click(syncButton);

    expect(postCallCount).toBe(1);
    expect(postBodyCaptured).toBeUndefined(); // No source_id in body
    expect(syncButton).toBeDisabled();

    // Fail this post to check error display
    act(() => {
      rejectPost(new ApiError("tracker_unreachable", "Tracker unreachable", 500));
    });

    expect(await screen.findByText(/Tracker unreachable/i)).toBeInTheDocument();
    expect(syncButton).toBeEnabled();

    // Now emit a tracking.synced event through FakeStream
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

    // Both sources and runs refetched
    await waitFor(() => {
      expect(sourcesCallCount).toBeGreaterThanOrEqual(2);
      expect(runsCallCount).toBeGreaterThanOrEqual(2);
    });
  });

  it("shows source last_error in a destructive Alert above the still-listed rows", async () => {
    const sources = [
      makeSource({
        id: "trk_err",
        type: "mlflow",
        last_error: "Connection timed out connecting to MLflow at http://mlflow:5000",
        last_sync: "2026-10-29T08:00:00Z",
      }),
    ];
    const runsPage: Page_RunRow_ = {
      rows: [makeRun({ key: "good_run", experiment: "prior-good-experiment" })],
      total: 1,
      truncated: false,
    };
    const ciPage: Page_CiRunRow_ = {
      rows: [],
      total: 0,
      truncated: false,
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
      if (path === "/api/projects/{project_id}/tracking/runs") {
        return Promise.resolve({ data: runsPage, response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/tracking/ci-runs") {
        return Promise.resolve({ data: ciPage, response: new Response(null, { status: 200 }) });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/runs");

    // Last error displayed in Alert
    expect(
      await screen.findByText(/Connection timed out connecting to MLflow/i),
    ).toBeInTheDocument();
    // And prior good row is still displayed below it
    expect(screen.getByText("prior-good-experiment")).toBeInTheDocument();
  });

  it("shows 'No tracking source yet' empty state when sources is empty", async () => {
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
      if (path === "/api/projects/{project_id}/tracking/runs") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/ci-runs") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/runs");

    expect(
      await screen.findByText(
        "No tracking source yet. Onboarding creates them when you approve the handoff.",
      ),
    ).toBeInTheDocument();
  });

  it("shows 'Nothing tracked yet. Use Sync now.' when sources exist but rows are empty", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/sources") {
        return Promise.resolve({
          data: [makeSource({ last_sync: null })],
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/runs") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/ci-runs") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/runs");

    expect(await screen.findByText("Never synced")).toBeInTheDocument();
    expect(await screen.findByText("Nothing tracked yet. Use Sync now.")).toBeInTheDocument();
  });

  it("shows route error text when a query fails", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/sources") {
        return Promise.reject(new ApiError("db_error", "Database connection dropped", 500));
      }
      if (path === "/api/projects/{project_id}/tracking/runs") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/ci-runs") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/runs");

    expect(await screen.findByText(/Database connection dropped/i)).toBeInTheDocument();
  });

  it("shows 'Showing the latest N of TOTAL' when a page has truncated: true", async () => {
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
      if (path === "/api/projects/{project_id}/tracking/runs") {
        return Promise.resolve({
          data: {
            rows: [makeRun({ key: "r1" }), makeRun({ key: "r2" })],
            total: 350,
            truncated: true,
          },
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/tracking/ci-runs") {
        return Promise.resolve({
          data: { rows: [], total: 0, truncated: false },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/runs");

    expect(await screen.findByText(/Showing the latest 2 of 350/i)).toBeInTheDocument();
  });
});
