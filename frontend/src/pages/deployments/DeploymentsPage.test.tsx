import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { components } from "@/api/schema";
import { api, ApiError } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import { TestFakeStream } from "@/pages/inbox/testing";
import { project, stubProjects } from "@/test/projects";
import { renderPage } from "@/test/render";
import { route } from "./route";

type Deployments = components["schemas"]["DeploymentsView"];
type Decision = components["schemas"]["DecisionRow"];
type ReportView = components["schemas"]["ReportView"];

const REPORT: ReportView = {
  id: "rep_1",
  author: "agent_retraining",
  created: "2026-10-29T09:30:00Z",
  draft: {
    model: "churn",
    candidate_version: "3",
    fix_id: null,
    training_request_id: null,
    assessments: { leakage: { value: "Checked", not_applicable_reason: null } },
  },
  evaluation: {
    record: {
      source_id: "src",
      model: "churn",
      version: "3",
      run_key: "run",
      schema_version: 1,
      payload: {},
      fetched_at: "2026-10-29T09:00:00Z",
    },
    payload: {
      schema_version: 1,
      evaluation_commit: "abc1234",
      gate_config_version: "g1",
      champion_version: "2",
      decision: "promote",
      reasons: [],
      computed_where: "ci_step",
    },
    not_computed: {},
  },
};

function decision(overrides: Partial<Decision>): Decision {
  return {
    at: "2026-10-30T10:00:00Z",
    model: "churn",
    kind: "promotion",
    from_version: "2",
    to_version: "3",
    decided_by: "team_gate",
    decided_by_ref: null,
    report_id: null,
    ...overrides,
  };
}

const FULL: Deployments = {
  environments: [
    { model: "churn", environment: "production", alias: "champion", version: "3" },
    { model: "churn", environment: "staging", alias: "challenger", version: null },
    { model: "fraud", environment: "production", alias: "champion", version: "7" },
  ],
  decisions: [
    decision({ kind: "promotion", decided_by: "person", report_id: "rep_1", to_version: "3" }),
    decision({
      kind: "promotion",
      model: "fraud",
      decided_by: "rule",
      decided_by_ref: "rul_auto",
      from_version: "6",
      to_version: "7",
    }),
    decision({ kind: "rollback", decided_by: "team_gate", from_version: "3", to_version: "2" }),
    decision({
      kind: "fall_back",
      decided_by: "agent_bypass",
      from_version: null,
      to_version: null,
    }),
    decision({
      kind: "promotion",
      model: "fraud",
      decided_by: "person",
      decided_by_ref: "apr_9",
      from_version: "5",
      to_version: "6",
    }),
  ],
  open_prs: [
    { number: 12, title: "Retrain churn", agent: "retraining", labels: ["mlops-agents", "model"] },
  ],
};

function rowAt(rows: HTMLElement[], index: number): HTMLElement {
  const row = rows[index];
  if (!row) throw new Error(`No row ${String(index)}`);
  return row;
}

const EMPTY: Deployments = { environments: [], decisions: [], open_prs: [] };

function ok<T>(data: T) {
  return Promise.resolve({ data, response: new Response(null, { status: 200 }) });
}

describe("DeploymentsPage", () => {
  let fakeStream: TestFakeStream;
  let getSpy: MockInstance | undefined;
  let openStreamSpy: MockInstance;
  let githubFullName: string | null;

  beforeEach(() => {
    githubFullName = "org/repo";
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
    openStreamSpy.mockRestore();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stub(
    deployments: () => Promise<unknown>,
    report: () => Promise<unknown> = () => ok(REPORT),
  ) {
    getSpy = vi.spyOn(api, "GET").mockImplementation(((path: string) => {
      if (path === "/api/projects/{project_id}/deployments") return deployments();
      if (path === "/api/projects/{project_id}/reports/{report_id}") return report();
      if (path === "/api/projects/{project_id}") {
        return ok({ ...project("prj_1", "Test Project", null), github_full_name: githubFullName });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    }) as never);
  }

  function count(path: string): number {
    return (getSpy?.mock.calls ?? []).filter((c) => c[0] === path).length;
  }

  it("is a project nav entry named Deployments at order 40", () => {
    expect(route.path).toBe("/projects/:projectId/deployments");
    expect(route.nav).toMatchObject({ area: "project", label: "Deployments", order: 40 });
  });

  it("shows the version in each environment, grouped by model", async () => {
    stub(() => ok(FULL));
    renderPage("/projects/prj_1/deployments");

    const churn = await screen.findByTestId("environments-churn");
    expect(within(churn).getByText("production")).toBeInTheDocument();
    expect(within(churn).getByText("champion")).toBeInTheDocument();
    expect(within(churn).getByText("3")).toBeInTheDocument();
    expect(within(churn).getByText("No version yet")).toBeInTheDocument();
    const fraud = screen.getByTestId("environments-fraud");
    expect(within(fraud).getByText("7")).toBeInTheDocument();
  });

  it("shows each kind of decision with who decided, in the server's order", async () => {
    stub(() => ok(FULL));
    renderPage("/projects/prj_1/deployments");

    const rows = await screen.findAllByTestId("decision-row");
    expect(rows).toHaveLength(5);
    expect(within(rowAt(rows, 0)).getByText("Promotion")).toBeInTheDocument();
    expect(within(rowAt(rows, 0)).getByText("Person")).toBeInTheDocument();
    expect(within(rowAt(rows, 0)).getByText("2 → 3")).toBeInTheDocument();
    expect(within(rowAt(rows, 1)).getByText("Rule rul_auto")).toBeInTheDocument();
    expect(within(rowAt(rows, 2)).getByText("Rollback")).toBeInTheDocument();
    expect(within(rowAt(rows, 2)).getByText("Team gate")).toBeInTheDocument();
    expect(within(rowAt(rows, 3)).getByText("Fall-back")).toBeInTheDocument();
    expect(within(rowAt(rows, 3)).getByText("Agent bypass")).toBeInTheDocument();
    expect(within(rowAt(rows, 3)).getByText("unknown → unknown")).toBeInTheDocument();
    expect(within(rowAt(rows, 4)).getByText("Person (approval apr_9)")).toBeInTheDocument();
  });

  it("offers a Report button only where the decision has a report, and fetches it lazily", async () => {
    const user = userEvent.setup();
    stub(() => ok(FULL));
    renderPage("/projects/prj_1/deployments");

    const rows = await screen.findAllByTestId("decision-row");
    expect(screen.getAllByRole("button", { name: "Report" })).toHaveLength(1);
    expect(count("/api/projects/{project_id}/reports/{report_id}")).toBe(0);

    await user.click(within(rowAt(rows, 0)).getByRole("button", { name: "Report" }));

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("computed by team step (ci_step)")).toBeInTheDocument();
    expect(within(dialog).getByText("abc1234")).toBeInTheDocument();
    expect(within(dialog).getByText("Checked")).toBeInTheDocument();
  });

  it("shows the server's message when the report is not found", async () => {
    const user = userEvent.setup();
    stub(
      () => ok(FULL),
      () =>
        Promise.reject(new ApiError("report_not_found", "No report rep_1 in this project", 404)),
    );
    renderPage("/projects/prj_1/deployments");

    const rows = await screen.findAllByTestId("decision-row");
    await user.click(within(rowAt(rows, 0)).getByRole("button", { name: "Report" }));

    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("No report rep_1 in this project")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("links open pull requests on GitHub with their agent and labels", async () => {
    stub(() => ok(FULL));
    renderPage("/projects/prj_1/deployments");

    const link = await screen.findByRole("link", { name: /#12 Retrain churn/ });
    expect(link).toHaveAttribute("href", "https://github.com/org/repo/pull/12");
    expect(link).toHaveAttribute("target", "_blank");
    const item = screen.getByTestId("open-pr-12");
    expect(within(item).getByText("retraining")).toBeInTheDocument();
    expect(within(item).getByText("model")).toBeInTheDocument();
  });

  it("shows pull requests without a link for a local project", async () => {
    githubFullName = null;
    stub(() => ok(FULL));
    renderPage("/projects/prj_1/deployments");

    const item = await screen.findByTestId("open-pr-12");
    expect(within(item).getByText("#12 Retrain churn")).toBeInTheDocument();
    expect(within(item).queryByRole("link")).toBeNull();
  });

  it("explains what produces the data when nothing is deployed", async () => {
    stub(() => ok(EMPTY));
    renderPage("/projects/prj_1/deployments");

    expect(await screen.findByText(/Nothing is deployed yet/)).toBeInTheDocument();
    expect(screen.queryByText("Environments")).toBeNull();
  });

  it("gives each empty section its own line when only some have data", async () => {
    stub(() => ok({ ...EMPTY, open_prs: FULL.open_prs }));
    renderPage("/projects/prj_1/deployments");

    expect(await screen.findByText("No environments to show yet.")).toBeInTheDocument();
    expect(screen.getByText("No promotions or rollbacks yet.")).toBeInTheDocument();
    expect(screen.queryByText(/Nothing is deployed yet/)).toBeNull();
  });

  it("shows the server's error and Retry loads the page", async () => {
    const user = userEvent.setup();
    let calls = 0;
    stub(() => {
      calls += 1;
      return calls === 1
        ? Promise.reject(new ApiError("tracker_down", "The tracker is not reachable", 502))
        : ok(FULL);
    });
    renderPage("/projects/prj_1/deployments");

    expect(await screen.findByText("The tracker is not reachable")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("environments-churn")).toBeInTheDocument();
  });

  it.each(["promotion.applied", "promotion.refused", "hook.ran", "pr.merged_by_agent"] as const)(
    "refetches on %s",
    async (type) => {
      let calls = 0;
      stub(() => {
        calls += 1;
        return ok(calls === 1 ? EMPTY : FULL);
      });
      renderPage("/projects/prj_1/deployments");
      await screen.findByText(/Nothing is deployed yet/);

      act(() => {
        fakeStream.emitOpen();
        fakeStream.emit(
          type,
          {
            id: "evt_1",
            seq: 1,
            ts: "2026-10-30T10:00:02Z",
            source: "user",
            type,
            subject_id: null,
            payload: {},
          },
          1,
        );
      });

      expect(await screen.findByTestId("environments-churn")).toBeInTheDocument();
    },
  );

  it("does not refetch on an unrelated event", async () => {
    stub(() => ok(EMPTY));
    renderPage("/projects/prj_1/deployments");
    await screen.findByText(/Nothing is deployed yet/);

    act(() => {
      fakeStream.emitOpen();
      fakeStream.emit(
        "note.saved",
        {
          id: "evt_1",
          seq: 1,
          ts: "2026-10-30T10:00:02Z",
          source: "user",
          type: "note.saved",
          subject_id: null,
          payload: {},
        },
        1,
      );
    });
    await new Promise((r) => setTimeout(r, 300));

    expect(count("/api/projects/{project_id}/deployments")).toBe(1);
  });
});
