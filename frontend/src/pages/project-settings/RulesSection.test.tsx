import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { RulesSection } from "./RulesSection";
import { failure, json, renderSection, stubEventStream, stubRoutes } from "./testing";

type Rule = components["schemas"]["RuleView"];
type Hook = components["schemas"]["HookView"];

const BASE = "/api/projects/prj_demo/rules";
const HOOKS = "/api/projects/prj_demo/hooks";

const ACTIVE: Rule = {
  id: "rul_1",
  spec: {
    name: "Roll back on drift",
    why: "Drift above 0.5 hurts <b>revenue</b>.",
    model: "churn",
    terms: [
      {
        check_id: "drift_check",
        model: "churn",
        when: { op: "gt", field: "value", value: 0.5 },
        window_minutes: 60,
        quantifier: "latest",
        min_records: 1,
      },
    ],
    hook_id: "hok_rollback",
    hook_kind: "rollback",
    limits: { per_incident: 1, per_day: 2 },
  },
  enabled: true,
  proposal_id: "prop_1",
  approved: "2026-10-30T09:00:00Z",
};
const DISABLED: Rule = {
  ...ACTIVE,
  id: "rul_2",
  spec: { ...ACTIVE.spec, name: "Re-run nightly", model: null, hook_id: "hok_other" },
  enabled: false,
};
const HOOK = { id: "hok_rollback", name: "Rollback churn" } as Hook;

let stream: ReturnType<typeof stubEventStream>;

beforeEach(() => {
  stream = stubEventStream();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function setup(options: { rules?: Rule[]; routes?: Parameters<typeof stubRoutes>[0] } = {}) {
  let rules = options.rules ?? [ACTIVE, DISABLED];
  stubRoutes({
    [`GET ${BASE}`]: () => json(rules),
    [`GET ${HOOKS}`]: () => json([HOOK]),
    ...options.routes,
  });
  renderSection(<RulesSection projectId="prj_demo" />);
  return {
    user: userEvent.setup(),
    setRules: (next: Rule[]) => {
      rules = next;
    },
  };
}

describe("RulesSection", () => {
  it("renders each rule with its why, model, hook, limits and terms", async () => {
    setup();

    const first = within(await screen.findByRole("listitem", { name: "Roll back on drift" }));
    expect(first.getByText("Active")).toBeInTheDocument();
    expect(first.getByText("churn", { selector: "dd" })).toBeInTheDocument();
    expect(first.getByText("Drift above 0.5 hurts <b>revenue</b>.")).toBeInTheDocument();
    expect(await first.findByText("Rollback churn")).toBeInTheDocument();
    expect(first.getByText(/1 per incident, 2 per day/)).toBeInTheDocument();
    expect(first.getByText("drift_check")).toBeInTheDocument();
    expect(first.getByText("value gt 0.5")).toBeInTheDocument();
    const second = within(screen.getByRole("listitem", { name: "Re-run nightly" }));
    expect(second.getByText("Disabled")).toBeInTheDocument();
    expect(second.getByText("Project-wide", { selector: "dd" })).toBeInTheDocument();
    expect(second.getByText("hok_other")).toBeInTheDocument();
  });

  it("says where rules come from when there are none", async () => {
    setup({ rules: [] });

    expect(await screen.findByText("No rules yet.")).toBeInTheDocument();
    expect(screen.getByText(/approved proposals/)).toBeInTheDocument();
  });

  it("shows the server's error with a retry when the list cannot load", async () => {
    setup({ routes: { [`GET ${BASE}`]: () => failure("Rules unavailable", 500) } });

    expect(await screen.findByText("Rules unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("disables an enabled rule and the row updates", async () => {
    const put = vi.fn<(body: unknown) => Response>(() => json({ ...ACTIVE, enabled: false }));
    const { user } = setup({ routes: { [`PUT ${BASE}/rul_1/enabled`]: put } });

    await screen.findByRole("listitem", { name: "Roll back on drift" });
    await user.click(screen.getByRole("button", { name: "Disable Roll back on drift" }));

    await waitFor(() => {
      expect(
        within(screen.getByRole("listitem", { name: "Roll back on drift" })).getByText("Disabled"),
      ).toBeInTheDocument();
    });
    expect(put).toHaveBeenCalledWith({ enabled: false });
  });

  it("enables a disabled rule", async () => {
    const put = vi.fn<(body: unknown) => Response>(() => json({ ...DISABLED, enabled: true }));
    const { user } = setup({ routes: { [`PUT ${BASE}/rul_2/enabled`]: put } });

    await screen.findByRole("listitem", { name: "Re-run nightly" });
    await user.click(screen.getByRole("button", { name: "Enable Re-run nightly" }));

    await waitFor(() => {
      expect(put).toHaveBeenCalledWith({ enabled: true });
    });
  });

  it("shows a refused change as the server wrote it", async () => {
    const { user } = setup({
      routes: { [`PUT ${BASE}/rul_1/enabled`]: () => failure("Rule not found.", 404) },
    });

    await screen.findByRole("listitem", { name: "Roll back on drift" });
    await user.click(screen.getByRole("button", { name: "Disable Roll back on drift" }));

    expect(await screen.findByText("Rule not found.")).toBeInTheDocument();
  });

  it.each(["rule.satisfied", "proposal.decided", "handoff.approved"] as const)(
    "refetches when %s arrives",
    async (type) => {
      const { setRules } = setup({ rules: [ACTIVE] });
      await screen.findByRole("listitem", { name: "Roll back on drift" });

      setRules([ACTIVE, DISABLED]);
      await act(async () => {
        stream.emit(
          type,
          {
            id: "evt_1",
            seq: 1,
            ts: "2026-10-31T10:00:00Z",
            source: "rules",
            subject_id: null,
            payload: {},
            type,
          },
          1,
        );
        await Promise.resolve();
      });

      expect(await screen.findByRole("listitem", { name: "Re-run nightly" })).toBeInTheDocument();
    },
  );
});
