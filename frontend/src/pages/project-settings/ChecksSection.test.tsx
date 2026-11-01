import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import type { ProjectEventType } from "@/lib/useProjectEvents";
import { ChecksSection } from "./ChecksSection";
import { failure, json, renderSection, stubEventStream, stubRoutes } from "./testing";

type State = components["schemas"]["ExpectationState"];
type Check = components["schemas"]["CheckView"];

const ROUTE = "/api/projects/prj_demo/expectations";
const CHECKS_ROUTE = "/api/projects/prj_demo/checks";

const FILE_CHECK: Check = {
  id: "chk_1",
  check_id: "psi_age",
  category: "drift",
  cadence: { cron: "0 6 * * *", every_minutes: null },
  timezone: "Europe/Paris",
  results_location: { connection: "warehouse", path_pattern: "results/psi_age/*.json" },
  pr_number: 41,
  agent: "monitor",
  version: "v3",
  status: "active",
  registered: "2026-10-30T08:00:00Z",
};
const MLFLOW_CHECK: Check = {
  id: "chk_2",
  check_id: "auc_floor",
  category: "model_performance",
  cadence: { cron: null, every_minutes: 30 },
  timezone: "UTC",
  results_location: { metric: "auc", threshold: 0.8, fires_when: "below" },
  pr_number: 42,
  agent: "monitor",
  version: null,
  status: "pending_pr",
  registered: "2026-10-31T08:00:00Z",
};
const RETIRED_CHECK: Check = {
  ...FILE_CHECK,
  id: "chk_3",
  check_id: "old_check",
  pr_number: 40,
  version: null,
  status: "retired",
};

const OK: State = {
  key: { kind: "feed", ref: "daily_scores", model: "churn" },
  category: "data_quality",
  active: true,
  next_deadline: "2026-11-01T06:00:00Z",
  last_seen: "2026-10-31T06:01:00Z",
  status: "ok",
  since: null,
};
const LATE: State = {
  key: { kind: "job", ref: "nightly_eval", model: null },
  category: "model_performance",
  active: true,
  next_deadline: null,
  last_seen: null,
  status: "late",
  since: "2026-10-31T07:00:00Z",
};
const PENDING: State = {
  key: { kind: "check", ref: "psi_age", model: "churn" },
  category: "drift",
  active: false,
  next_deadline: null,
  last_seen: null,
  status: "pending_pr",
  since: null,
};

let stream: ReturnType<typeof stubEventStream>;

beforeEach(() => {
  stream = stubEventStream();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function setup(
  states: State[] | Response = [OK, LATE, PENDING],
  registered: Check[] | Response = [],
) {
  let current = states;
  let currentChecks = registered;
  const requests = stubRoutes({
    [`GET ${ROUTE}`]: () => (current instanceof Response ? current : json(current)),
    [`GET ${CHECKS_ROUTE}`]: () =>
      currentChecks instanceof Response ? currentChecks : json(currentChecks),
  });
  renderSection(<ChecksSection projectId="prj_demo" />);
  return {
    user: userEvent.setup(),
    requests,
    set: (next: State[]) => {
      current = next;
    },
    setChecks: (next: Check[]) => {
      currentChecks = next;
    },
  };
}

function emit(type: ProjectEventType, seq: number) {
  return act(async () => {
    stream.emit(
      type,
      {
        id: `evt_${String(seq)}`,
        seq,
        ts: "2026-10-31T07:00:00Z",
        source: "checks",
        subject_id: null,
        payload: {},
        type,
      },
      seq,
    );
    await new Promise((resolve) => setTimeout(resolve, 250));
  });
}

const checkRequests = (requests: { path: string }[]) =>
  requests.filter((r) => r.path === CHECKS_ROUTE).length;

describe("ChecksSection", () => {
  it("renders each expectation with its kind, name, category and status as text", async () => {
    setup();

    const ok = within(await screen.findByRole("listitem", { name: "daily_scores" }));
    expect(ok.getByText("Feed")).toBeInTheDocument();
    expect(ok.getByText(/churn/)).toBeInTheDocument();
    expect(ok.getByText("Data quality")).toBeInTheDocument();
    expect(ok.getByText("On time")).toBeInTheDocument();
    expect(ok.getByTestId("expectation-next-deadline")).not.toHaveTextContent("–");
    expect(ok.getByTestId("expectation-last-seen")).not.toHaveTextContent("–");
    const late = within(screen.getByRole("listitem", { name: "nightly_eval" }));
    expect(late.getByText("Late")).toBeInTheDocument();
    expect(late.getByTestId("expectation-since")).not.toHaveTextContent("–");
    const pending = within(screen.getByRole("listitem", { name: "psi_age" }));
    expect(pending.getByText("Waiting for its pull request")).toBeInTheDocument();
    expect(pending.getByText("Inactive")).toBeInTheDocument();
  });

  it("says so when nothing is expected yet", async () => {
    setup([]);

    expect(await screen.findByText("Nothing is expected yet.")).toBeInTheDocument();
  });

  it("shows the server's error with a retry", async () => {
    setup(failure("Expectations unavailable", 500));

    expect(await screen.findByText("Expectations unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("refetches when results are collected", async () => {
    const { set } = setup([OK]);
    await screen.findByRole("listitem", { name: "daily_scores" });

    set([OK, LATE]);
    await act(async () => {
      stream.emit(
        "results.collected",
        {
          id: "evt_1",
          seq: 1,
          ts: "2026-10-31T07:00:00Z",
          source: "results",
          subject_id: null,
          payload: {},
          type: "results.collected",
        },
        1,
      );
      await Promise.resolve();
    });

    expect(await screen.findByRole("listitem", { name: "nightly_eval" })).toBeInTheDocument();
  });

  describe("registered checks", () => {
    it("renders each status with cadence, time zone, PR, agent and location as text", async () => {
      setup([], [FILE_CHECK, MLFLOW_CHECK, RETIRED_CHECK]);

      const file = within(await screen.findByRole("listitem", { name: "Check psi_age" }));
      expect(file.getByText("Active")).toBeInTheDocument();
      expect(file.getByText("Drift")).toBeInTheDocument();
      expect(file.getByText("0 6 * * *")).toBeInTheDocument();
      expect(file.getByText("Europe/Paris")).toBeInTheDocument();
      expect(file.getByText("#41")).toBeInTheDocument();
      expect(file.getByText("monitor")).toBeInTheDocument();
      expect(file.getByText("v3")).toBeInTheDocument();
      expect(file.getByText(/warehouse/)).toBeInTheDocument();
      expect(file.getByText(/results\/psi_age\/\*\.json/)).toBeInTheDocument();

      const mlflow = within(screen.getByRole("listitem", { name: "Check auc_floor" }));
      expect(mlflow.getByText("Waiting for its pull request")).toBeInTheDocument();
      expect(mlflow.getByText("every 30 minutes")).toBeInTheDocument();
      expect(mlflow.getByText("MLflow metric auc, fires when below 0.8")).toBeInTheDocument();
      expect(mlflow.queryByText("Version")).not.toBeInTheDocument();

      const retired = within(screen.getByRole("listitem", { name: "Check old_check" }));
      expect(retired.getByText("Retired")).toBeInTheDocument();
    });

    it("keeps the server's order", async () => {
      setup([], [MLFLOW_CHECK, FILE_CHECK]);

      await screen.findByRole("listitem", { name: "Check psi_age" });
      const names = screen
        .getAllByRole("listitem")
        .map((item) => item.getAttribute("aria-label"))
        .filter((name) => name?.startsWith("Check "));
      expect(names).toEqual(["Check auc_floor", "Check psi_age"]);
    });

    it("explains where checks come from when there are none", async () => {
      setup();

      expect(
        await screen.findByText(/Agents register checks through a pull request/),
      ).toBeInTheDocument();
    });

    it("shows the server's error with a retry while the expectation states still render", async () => {
      const { user, setChecks } = setup([OK], failure("Checks unavailable", 500));

      expect(await screen.findByText("Checks unavailable")).toBeInTheDocument();
      expect(screen.getByRole("listitem", { name: "daily_scores" })).toBeInTheDocument();

      setChecks([FILE_CHECK]);
      await user.click(screen.getByRole("button", { name: "Retry" }));

      expect(await screen.findByRole("listitem", { name: "Check psi_age" })).toBeInTheDocument();
    });

    it("keeps the checks when the expectation states fail", async () => {
      setup(failure("Expectations unavailable", 500), [FILE_CHECK]);

      expect(await screen.findByText("Expectations unavailable")).toBeInTheDocument();
      expect(await screen.findByRole("listitem", { name: "Check psi_age" })).toBeInTheDocument();
    });

    it("refetches the checks on check.registered and not on an unrelated event", async () => {
      const { requests, setChecks } = setup([], [FILE_CHECK]);
      await screen.findByRole("listitem", { name: "Check psi_age" });
      expect(checkRequests(requests)).toBe(1);

      await emit("pr.updated", 1);
      expect(checkRequests(requests)).toBe(1);

      setChecks([FILE_CHECK, MLFLOW_CHECK]);
      await emit("check.registered", 2);

      expect(await screen.findByRole("listitem", { name: "Check auc_floor" })).toBeInTheDocument();
      expect(checkRequests(requests)).toBe(2);
    });

    it.each(["task.ended", "results.collected"] as const)(
      "refetches the checks on %s",
      async (type) => {
        const { requests, setChecks } = setup([], [MLFLOW_CHECK]);
        await screen.findByRole("listitem", { name: "Check auc_floor" });

        setChecks([{ ...MLFLOW_CHECK, status: "active", version: "v9" }]);
        await emit(type, 3);

        expect(await screen.findByText("v9")).toBeInTheDocument();
        expect(checkRequests(requests)).toBe(2);
      },
    );
  });
});
