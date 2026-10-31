import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderPage } from "@/test/render";
import {
  CLOSE,
  emitEvent,
  eventRecord,
  failure,
  incident,
  INCIDENT,
  ok,
  signal,
  SIGNALS,
  stubGet,
  stubPost,
  stubStream,
  TIMELINE,
} from "./testing";

const FULL = incident("inc_1", {
  category: "data_quality",
  subtype: "schema_change",
  severity: "critical",
  status: "acknowledged",
  cause: "Upstream renamed a column",
  model: "churn",
  held: true,
  resolved_offline: true,
  reopened_count: 2,
  merged_into: "inc_9",
  prs: [12],
  issues: [34],
  started_at: "2026-10-30T09:00:00Z",
  detected_at: "2026-10-30T09:05:00Z",
  acknowledged_at: "2026-10-30T09:10:00Z",
  verdict: {
    incident_id: "inc_1",
    value: "wrong_type",
    right_category: "drift",
    right_subtype: "covariate",
    source: "user",
    check_versions: {},
    note: "It was drift, not a schema change",
    created: "2026-10-30T09:20:00Z",
  },
});

function stubPage(timeline: (init: never) => Promise<unknown> = () => ok([])) {
  return stubGet({
    [INCIDENT]: () => ok(FULL),
    [SIGNALS]: () => ok([signal("sig_1", { detail: { message: "<script>alert(1)</script>" } })]),
    [TIMELINE]: timeline,
  });
}

describe("IncidentPage", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("renders the incident's fields, signals, related items and verdict", async () => {
    stubStream();
    stubPage();

    renderPage("/projects/prj_1/incidents/inc_1");

    expect(await screen.findByText("Upstream renamed a column")).toBeInTheDocument();
    const header = within(screen.getByTestId("incident-header"));
    expect(header.getByText("Critical")).toBeInTheDocument();
    expect(header.getByText("Data quality")).toBeInTheDocument();
    expect(header.getByText("schema_change")).toBeInTheDocument();
    expect(header.getByText("Acknowledged")).toBeInTheDocument();
    const facts = within(screen.getByTestId("incident-facts"));
    expect(facts.getByText("Held")).toBeInTheDocument();
    expect(facts.getByTestId("fact-held")).toHaveTextContent("Yes");
    expect(facts.getByTestId("fact-resolved-offline")).toHaveTextContent("Yes");
    expect(facts.getByTestId("fact-reopened")).toHaveTextContent("2");
    expect(facts.getByRole("link", { name: "inc_9" })).toHaveAttribute(
      "href",
      "/projects/prj_1/incidents/inc_9",
    );
    expect(facts.getByText("#12")).toBeInTheDocument();
    expect(facts.getByText("#34")).toBeInTheDocument();
    for (const label of [
      "Started",
      "Detected",
      "Acknowledged",
      "Mitigated",
      "Resolved",
      "Closed",
    ]) {
      expect(facts.getByText(label)).toBeInTheDocument();
    }
    const signals = await screen.findByTestId("signal-sig_1");
    expect(within(signals).getByText("features.age")).toBeInTheDocument();
    expect(within(signals).getByText("ci/run/42")).toBeInTheDocument();
    const verdict = screen.getByTestId("incident-verdict");
    expect(within(verdict).getByText("Wrong type")).toBeInTheDocument();
    expect(within(verdict).getByText(/Drift/)).toBeInTheDocument();
    expect(within(verdict).getByText("It was drift, not a schema change")).toBeInTheDocument();
  });

  it("shows a signal's detail and a note as plain text, never as HTML", async () => {
    stubStream();
    stubPage();

    const { container } = renderPage("/projects/prj_1/incidents/inc_1");

    expect(await screen.findByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
  });

  it("labels timeline entries by type, including wake_skipped and suspect_linked, and survives an unknown type", async () => {
    stubStream();
    stubPage(() =>
      ok([
        eventRecord(5, "incident.wake_skipped", { reason: "held" }),
        eventRecord(4, "incident.suspect_linked"),
        eventRecord(3, "hook.failed"),
        eventRecord(2, "rule.satisfied"),
        eventRecord(1, "totally.new.type" as never),
      ]),
    );

    renderPage("/projects/prj_1/incidents/inc_1");

    const timeline = await screen.findByTestId("incident-timeline");
    const entries = within(timeline).getAllByRole("listitem");
    expect(entries).toHaveLength(5);
    expect(within(entries[0] as HTMLElement).getByText("Agent wake skipped")).toBeInTheDocument();
    expect(
      within(entries[1] as HTMLElement).getByText("Suspect change linked"),
    ).toBeInTheDocument();
    expect(within(entries[2] as HTMLElement).getByText("Hook failed")).toBeInTheDocument();
    expect(within(entries[3] as HTMLElement).getByText("Rule satisfied")).toBeInTheDocument();
    expect(within(entries[4] as HTMLElement).getByText("totally.new.type")).toBeInTheDocument();
  });

  it("loads the timeline's next page with the last seq as the cursor", async () => {
    const user = userEvent.setup();
    stubStream();
    const first = Array.from({ length: 25 }, (_, i) => eventRecord(100 - i, "signal.raised"));
    const get = stubPage((init: never) =>
      (init as { params: { query: { before?: number } } }).params.query.before === undefined
        ? ok(first)
        : ok([eventRecord(7, "incident.closed")]),
    );

    renderPage("/projects/prj_1/incidents/inc_1");
    await screen.findByTestId("incident-timeline");
    await user.click(await screen.findByRole("button", { name: "Load older events" }));

    expect(await screen.findByText("Incident closed")).toBeInTheDocument();
    const queries = get.mock.calls
      .filter((c) => c[0] === TIMELINE)
      .map((c) => (c[1] as { params: { query: { before?: number } } }).params.query.before);
    expect(queries).toContain(76);
    expect(screen.queryByRole("button", { name: "Load older events" })).toBeNull();
  });

  it("refetches the incident when an incident event arrives", async () => {
    const fake = stubStream();
    const get = stubPage();
    renderPage("/projects/prj_1/incidents/inc_1");
    await screen.findByText("Upstream renamed a column");
    const before = get.mock.calls.filter((c) => c[0] === INCIDENT).length;

    act(() => {
      emitEvent(fake, eventRecord(1, "incident.updated"));
    });

    await waitFor(
      () => {
        expect(get.mock.calls.filter((c) => c[0] === INCIDENT).length).toBeGreaterThan(before);
      },
      { timeout: 3000 },
    );
  });

  it("closes the incident as a separate person action and shows a refusal as is", async () => {
    const user = userEvent.setup();
    stubStream();
    stubPage();
    const post = stubPost({
      [CLOSE]: () => failure("incident_open", "Record a verdict first.", 409),
    });

    renderPage("/projects/prj_1/incidents/inc_1");
    await user.click(await screen.findByRole("button", { name: "Close incident" }));

    expect(await screen.findByText("Record a verdict first.")).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith(
      CLOSE,
      expect.objectContaining({ params: { path: { project_id: "prj_1", incident_id: "inc_1" } } }),
    );
  });

  it("shows not found for an unknown incident", async () => {
    stubStream();
    stubGet({
      [INCIDENT]: () => failure("incident_not_found", "Incident 'inc_x' not found.", 404),
      [SIGNALS]: () => failure("incident_not_found", "Incident 'inc_x' not found.", 404),
      [TIMELINE]: () => failure("incident_not_found", "Incident 'inc_x' not found.", 404),
    });

    renderPage("/projects/prj_1/incidents/inc_x");

    expect(await screen.findByRole("heading", { name: "Incident not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to the Inbox" })).toHaveAttribute(
      "href",
      "/projects/prj_1/inbox",
    );
  });

  it("shows an error with a retry for any other failure", async () => {
    const user = userEvent.setup();
    stubStream();
    let fail = true;
    stubGet({
      [INCIDENT]: () => (fail ? failure("boom", "The server failed.", 500) : ok(FULL)),
      [SIGNALS]: () => ok([]),
      [TIMELINE]: () => ok([]),
    });

    renderPage("/projects/prj_1/incidents/inc_1");
    expect(await screen.findByText("The server failed.")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("Upstream renamed a column")).toBeInTheDocument();
  });
});
