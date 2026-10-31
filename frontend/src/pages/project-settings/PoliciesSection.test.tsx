import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { PoliciesSection } from "./PoliciesSection";
import { failure, json, renderSection, stubEventStream, stubRoutes } from "./testing";

type Policy = components["schemas"]["TrainingPolicyView"];

const BASE = "/api/projects/prj_demo/training/policies";

const ACTIVE: Policy = {
  id: "pol_1",
  payload: {
    model: "churn",
    train_hook_id: "hook_train",
    evaluate_hook_id: "hook_eval",
    window: { kind: "trailing_days", days: 30, start: null, end: null },
    min_interval_hours: 24,
    max_runs_per_week: 3,
    run_time_limit_minutes: 90,
  },
  status: "active",
  suspended_reason: null,
  proposal_id: "prop_1",
  approved: "2026-10-30T09:00:00Z",
};
const SUSPENDED: Policy = {
  ...ACTIVE,
  id: "pol_2",
  payload: {
    ...ACTIVE.payload,
    model: "fraud",
    window: { kind: "since_last_run", days: null, start: null, end: null },
  },
  status: "suspended",
  suspended_reason: "Its train hook is suspended.",
};

let stream: ReturnType<typeof stubEventStream>;

beforeEach(() => {
  stream = stubEventStream();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function setup(options: { policies?: Policy[]; routes?: Parameters<typeof stubRoutes>[0] } = {}) {
  let policies = options.policies ?? [ACTIVE, SUSPENDED];
  stubRoutes({ [`GET ${BASE}`]: () => json(policies), ...options.routes });
  renderSection(<PoliciesSection projectId="prj_demo" />);
  return {
    user: userEvent.setup(),
    setPolicies: (next: Policy[]) => {
      policies = next;
    },
  };
}

describe("PoliciesSection", () => {
  it("renders each policy with its status, reason, window and limits", async () => {
    setup();

    const first = within(await screen.findByRole("listitem", { name: "Policy for churn" }));
    expect(first.getByText("Active")).toBeInTheDocument();
    expect(first.getByText(/hook_train/)).toBeInTheDocument();
    expect(first.getByText(/hook_eval/)).toBeInTheDocument();
    expect(first.getByText("Trailing 30 days")).toBeInTheDocument();
    expect(first.getByText(/24 hours/)).toBeInTheDocument();
    expect(first.getByText(/3 runs per week/)).toBeInTheDocument();
    expect(first.getByText(/90 minutes/)).toBeInTheDocument();
    const second = within(screen.getByRole("listitem", { name: "Policy for fraud" }));
    expect(second.getByText("Suspended")).toBeInTheDocument();
    expect(second.getByText("Its train hook is suspended.")).toBeInTheDocument();
    expect(second.getByText("Since the last run")).toBeInTheDocument();
  });

  it("says so when there are no policies", async () => {
    setup({ policies: [] });

    expect(await screen.findByText("No training policies yet.")).toBeInTheDocument();
  });

  it("shows the server's error with a retry when the list cannot load", async () => {
    setup({ routes: { [`GET ${BASE}`]: () => failure("Policies unavailable", 500) } });

    expect(await screen.findByText("Policies unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("disables an active policy and the row updates", async () => {
    const put = vi.fn<(body: unknown) => Response>(() => json({ ...ACTIVE, status: "disabled" }));
    const { user } = setup({ routes: { [`PUT ${BASE}/pol_1/enabled`]: put } });

    const first = await screen.findByRole("listitem", { name: "Policy for churn" });
    await user.click(within(first).getByRole("button", { name: "Disable" }));

    await waitFor(() => {
      expect(
        within(screen.getByRole("listitem", { name: "Policy for churn" })).getByText("Disabled"),
      ).toBeInTheDocument();
    });
    expect(put).toHaveBeenCalledWith({ enabled: false });
  });

  it("shows a refused change as the server wrote it", async () => {
    const { user } = setup({
      routes: { [`PUT ${BASE}/pol_2/enabled`]: () => failure("Approve the hook first.", 409) },
    });

    const second = await screen.findByRole("listitem", { name: "Policy for fraud" });
    await user.click(within(second).getByRole("button", { name: "Disable" }));

    expect(await screen.findByText("Approve the hook first.")).toBeInTheDocument();
  });

  it("refetches when a policy.suspended event arrives", async () => {
    const { setPolicies } = setup();
    await screen.findByRole("listitem", { name: "Policy for churn" });

    setPolicies([{ ...ACTIVE, status: "suspended", suspended_reason: "Limit reached." }]);
    await act(async () => {
      stream.emit(
        "policy.suspended",
        {
          id: "evt_1",
          seq: 1,
          ts: "2026-10-30T10:00:00Z",
          source: "training",
          subject_id: "pol_1",
          payload: {},
          type: "policy.suspended",
        },
        1,
      );
      await Promise.resolve();
    });

    expect(await screen.findByText("Limit reached.")).toBeInTheDocument();
  });
});
