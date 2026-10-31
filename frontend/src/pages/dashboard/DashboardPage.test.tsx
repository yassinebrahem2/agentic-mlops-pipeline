import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { components } from "@/api/schema";
import { api, ApiError } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import { TestFakeStream } from "@/pages/inbox/testing";
import { project, stubProjects } from "@/test/projects";
import { renderPage } from "@/test/render";
import { route } from "./route";

type DashboardView = components["schemas"]["DashboardView"];

function view(overrides: Partial<DashboardView> = {}): DashboardView {
  return {
    stages: [
      { name: "ingest", status: "ok", incident_ids: [] },
      { name: "prepare", status: "incident", incident_ids: ["inc_1", "inc_2"] },
      { name: "train", status: "late", incident_ids: [] },
      { name: "serve", status: "unknown", incident_ids: [] },
    ],
    edges: [
      { from_stage: "ingest", to_stage: "prepare" },
      { from_stage: "prepare", to_stage: "train" },
      { from_stage: "train", to_stage: "serve" },
    ],
    models: [
      {
        name: "cancel",
        production_version: "7",
        latest_metrics: { auc: 0.91 },
        open_incidents: 2,
      },
      { name: "noshow", production_version: null, latest_metrics: {}, open_incidents: 0 },
    ],
    incidents_by_severity: { critical: 1, high: 0, medium: 3, low: 0 },
    at_risk: [
      {
        key: { kind: "feed", ref: "bookings", model: null },
        category: "data_quality",
        active: true,
        status: "silent",
        next_deadline: null,
        last_seen: null,
        since: null,
      },
    ],
    agents: [
      { agent: "coder", state: "running", doing: "Fix the parser" },
      { agent: "analyst", state: "idle", doing: null },
    ],
    heartbeat_reachable: true,
    baseline_available: true,
    ...overrides,
  };
}

function ok(data: DashboardView) {
  return Promise.resolve({ data, response: new Response(null, { status: 200 }) });
}

describe("DashboardPage", () => {
  let fakeStream: TestFakeStream;
  let getSpy: MockInstance | undefined;

  beforeEach(() => {
    fakeStream = new TestFakeStream();
    vi.spyOn(streamModule, "openStream").mockImplementation(() => {
      setTimeout(() => {
        fakeStream.emitOpen();
      }, 0);
      return fakeStream;
    });
    stubProjects([project("prj_1", "Test Project", "2026-10-20T10:00:00Z")]);
  });

  afterEach(() => {
    getSpy?.mockRestore();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubDashboard(handler: () => Promise<unknown>) {
    getSpy = vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/api/projects/{project_id}/dashboard") return handler();
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: project("prj_1", "Test Project", null),
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    }) as never);
  }

  function dashboardCalls(): number {
    return (getSpy?.mock.calls ?? []).filter((c) => c[0] === "/api/projects/{project_id}/dashboard")
      .length;
  }

  it("is the first project page, named Dashboard", () => {
    expect(route.path).toBe("/projects/:projectId/dashboard");
    expect(route.nav).toMatchObject({ area: "project", label: "Dashboard", order: -10 });
  });

  it("renders the stages in edge order with status as text and the incident count", async () => {
    stubDashboard(() => ok(view()));
    renderPage("/projects/prj_1/dashboard");

    const map = await screen.findByRole("region", { name: "Pipeline map" });
    const stages = within(map).getAllByTestId(/^stage-/);
    expect(stages.map((s) => s.getAttribute("data-testid"))).toEqual([
      "stage-ingest",
      "stage-prepare",
      "stage-train",
      "stage-serve",
    ]);
    expect(within(screen.getByTestId("stage-ingest")).getByText("OK")).toBeInTheDocument();
    const prepare = screen.getByTestId("stage-prepare");
    expect(within(prepare).getByText("Incident")).toBeInTheDocument();
    expect(within(prepare).getByText("2 active incidents")).toBeInTheDocument();
    expect(within(prepare).queryByRole("link")).toBeNull();
    expect(within(screen.getByTestId("stage-train")).getByText("Late")).toBeInTheDocument();
    expect(within(screen.getByTestId("stage-serve")).getByText("Unknown")).toBeInTheDocument();
  });

  it("lists the edges between stages", async () => {
    stubDashboard(() => ok(view()));
    renderPage("/projects/prj_1/dashboard");

    const edges = await screen.findByRole("list", { name: "Pipeline edges" });
    expect(
      within(edges)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["ingest feeds prepare", "prepare feeds train", "train feeds serve"]);
  });

  it("renders model cards", async () => {
    stubDashboard(() => ok(view()));
    renderPage("/projects/prj_1/dashboard");

    const cancel = await screen.findByTestId("model-cancel");
    expect(within(cancel).getByText("Production version 7")).toBeInTheDocument();
    expect(within(cancel).getByText("auc")).toBeInTheDocument();
    expect(within(cancel).getByText("0.91")).toBeInTheDocument();
    expect(within(cancel).getByText("2 open incidents")).toBeInTheDocument();
    const noshow = screen.getByTestId("model-noshow");
    expect(within(noshow).getByText("No production version")).toBeInTheDocument();
    expect(within(noshow).getByText("No metrics yet")).toBeInTheDocument();
  });

  it("shows every severity from the API in the API's order, zero included", async () => {
    stubDashboard(() => ok(view()));
    renderPage("/projects/prj_1/dashboard");

    const list = await screen.findByRole("list", { name: "Active incidents by severity" });
    expect(
      within(list)
        .getAllByRole("listitem")
        .map((li) => li.textContent),
    ).toEqual(["critical1", "high0", "medium3", "low0"]);
  });

  it("lists at-risk expectations and what the agents are doing", async () => {
    stubDashboard(() => ok(view()));
    renderPage("/projects/prj_1/dashboard");

    const risk = await screen.findByTestId("at-risk-feed-bookings");
    expect(within(risk).getByText("bookings")).toBeInTheDocument();
    expect(within(risk).getByText("silent")).toBeInTheDocument();

    const coder = screen.getByTestId("agent-coder");
    expect(within(coder).getByText("running")).toBeInTheDocument();
    expect(within(coder).getByText("Fix the parser")).toBeInTheDocument();
    expect(within(screen.getByTestId("agent-analyst")).getByText("idle")).toBeInTheDocument();
  });

  it("says when nothing is at risk", async () => {
    stubDashboard(() => ok(view({ at_risk: [], agents: [] })));
    renderPage("/projects/prj_1/dashboard");
    expect(await screen.findByText("Nothing is due, late or silent.")).toBeInTheDocument();
    expect(screen.getByText("No agents yet.")).toBeInTheDocument();
  });

  it("shows no notice when the heartbeat and the baseline are available", async () => {
    stubDashboard(() => ok(view()));
    renderPage("/projects/prj_1/dashboard");
    await screen.findByRole("region", { name: "Pipeline map" });
    expect(screen.queryByText(/pages nobody/)).toBeNull();
    expect(screen.queryByText(/not readable yet/)).toBeNull();
  });

  it("shows the local-mode notice when the heartbeat cannot be reached", async () => {
    stubDashboard(() => ok(view({ heartbeat_reachable: false })));
    renderPage("/projects/prj_1/dashboard");
    expect(await screen.findByText(/pages nobody/)).toBeInTheDocument();
  });

  it("says the team's monitoring is not readable when there is no baseline", async () => {
    stubDashboard(() => ok(view({ baseline_available: false })));
    renderPage("/projects/prj_1/dashboard");
    expect(
      await screen.findByText(/team's own monitoring is not readable yet/),
    ).toBeInTheDocument();
  });

  it("explains the empty map before onboarding is approved", async () => {
    stubDashboard(() => ok(view({ stages: [], edges: [], models: [] })));
    renderPage("/projects/prj_1/dashboard");
    expect(
      await screen.findByText("The pipeline map appears once onboarding is approved."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Pipeline edges" })).toBeNull();
  });

  it("shows a loading state", () => {
    stubDashboard(() => new Promise(() => undefined));
    renderPage("/projects/prj_1/dashboard");
    expect(screen.getByText("Loading dashboard...")).toBeInTheDocument();
  });

  it("shows the error with a retry that refetches", async () => {
    const user = userEvent.setup();
    let calls = 0;
    stubDashboard(() => {
      calls += 1;
      return calls === 1
        ? Promise.reject(new ApiError("internal", "Backend is down", 500))
        : ok(view());
    });
    renderPage("/projects/prj_1/dashboard");

    expect(await screen.findByText("Backend is down")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByRole("region", { name: "Pipeline map" })).toBeInTheDocument();
  });

  it("refetches on an incident.opened event", async () => {
    stubDashboard(() => ok(view()));
    renderPage("/projects/prj_1/dashboard");
    await screen.findByRole("region", { name: "Pipeline map" });
    await waitFor(() => {
      expect(fakeStream.listeners.has("incident.opened")).toBe(true);
    });
    const before = dashboardCalls();

    act(() => {
      fakeStream.emitOpen();
      fakeStream.emit(
        "incident.opened",
        {
          id: "evt_1",
          seq: 1,
          ts: "2026-10-30T10:00:01Z",
          source: "system",
          type: "incident.opened",
          subject_id: "inc_1",
          payload: {},
        },
        1,
      );
    });

    await waitFor(() => {
      expect(dashboardCalls()).toBeGreaterThan(before);
    });
  });
});
