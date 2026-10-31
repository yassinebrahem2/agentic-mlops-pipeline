import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { ChecksSection } from "./ChecksSection";
import { failure, json, renderSection, stubEventStream, stubRoutes } from "./testing";

type State = components["schemas"]["ExpectationState"];

const ROUTE = "/api/projects/prj_demo/expectations";

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

function setup(states: State[] | Response = [OK, LATE, PENDING]) {
  let current = states;
  stubRoutes({ [`GET ${ROUTE}`]: () => (current instanceof Response ? current : json(current)) });
  renderSection(<ChecksSection projectId="prj_demo" />);
  return {
    user: userEvent.setup(),
    set: (next: State[]) => {
      current = next;
    },
  };
}

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
});
