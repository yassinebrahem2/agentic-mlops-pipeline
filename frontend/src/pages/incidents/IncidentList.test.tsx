import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderPage } from "@/test/render";
import {
  ACKNOWLEDGE,
  emitEvent,
  eventRecord,
  failure,
  incident,
  INCIDENTS,
  ok,
  pageOf,
  stubGet,
  stubPost,
  stubStream,
  VERDICT,
  bodyOf,
} from "./testing";

describe("Inbox incidents section", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("lists incidents in the order the route returns them, asking for the active ones", async () => {
    stubStream();
    const get = stubGet({
      [INCIDENTS]: () =>
        ok(
          pageOf([
            incident("inc_low_first", { severity: "low", cause: "Cause B" }),
            incident("inc_crit_second", { severity: "critical", cause: "Cause A" }),
          ]),
        ),
    });

    renderPage("/projects/prj_1/inbox");

    const rows = await screen.findAllByTestId(/^incident-row-/);
    expect(rows.map((r) => r.getAttribute("data-testid"))).toEqual([
      "incident-row-inc_low_first",
      "incident-row-inc_crit_second",
    ]);
    const first = within(rows[0] as HTMLElement);
    expect(first.getByText("Low")).toBeInTheDocument();
    expect(first.getByText("Drift")).toBeInTheDocument();
    expect(first.getByText("Cause B")).toBeInTheDocument();
    expect(first.getByRole("link", { name: /Cause B/ })).toHaveAttribute(
      "href",
      "/projects/prj_1/incidents/inc_low_first",
    );
    const query = (
      get.mock.calls.find((c) => c[0] === INCIDENTS)?.[1] as {
        params: { query: { status: string[] } };
      }
    ).params.query;
    expect(query.status).toEqual(["open", "acknowledged", "mitigated", "reopened"]);
  });

  it("acknowledges an incident: sends the request and the row stops offering it", async () => {
    const user = userEvent.setup();
    stubStream();
    let acknowledged = false;
    stubGet({
      [INCIDENTS]: () =>
        ok(
          pageOf([
            incident("inc_1", {
              acknowledged_at: acknowledged ? "2026-10-30T10:00:00Z" : null,
              status: acknowledged ? "acknowledged" : "open",
            }),
          ]),
        ),
    });
    const post = stubPost({
      [ACKNOWLEDGE]: () => {
        acknowledged = true;
        return ok(incident("inc_1", { acknowledged_at: "2026-10-30T10:00:00Z" }));
      },
    });

    renderPage("/projects/prj_1/inbox");
    const row = await screen.findByTestId("incident-row-inc_1");
    await user.click(within(row).getByRole("button", { name: "Acknowledge" }));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith(
        ACKNOWLEDGE,
        expect.objectContaining({
          params: { path: { project_id: "prj_1", incident_id: "inc_1" } },
        }),
      );
    });
    await waitFor(() => {
      expect(
        within(screen.getByTestId("incident-row-inc_1")).queryByRole("button", {
          name: "Acknowledge",
        }),
      ).toBeNull();
    });
    expect(
      within(screen.getByTestId("incident-row-inc_1")).getByText("Acknowledged"),
    ).toBeInTheDocument();
  });

  it("shows the server's refusal when acknowledging fails", async () => {
    const user = userEvent.setup();
    stubStream();
    stubGet({ [INCIDENTS]: () => ok(pageOf([incident("inc_1")])) });
    stubPost({ [ACKNOWLEDGE]: () => failure("incident_closed", "The incident is closed.", 409) });

    renderPage("/projects/prj_1/inbox");
    await user.click(await screen.findByRole("button", { name: "Acknowledge" }));

    expect(await screen.findByText("The incident is closed.")).toBeInTheDocument();
  });

  it("refetches when an incident.opened event arrives", async () => {
    const fake = stubStream();
    let rows = [incident("inc_1")];
    const get = stubGet({ [INCIDENTS]: () => ok(pageOf(rows)) });

    renderPage("/projects/prj_1/inbox");
    await screen.findByTestId("incident-row-inc_1");
    rows = [incident("inc_2", { cause: "New one" }), incident("inc_1")];
    const before = get.mock.calls.filter((c) => c[0] === INCIDENTS).length;

    act(() => {
      emitEvent(fake, eventRecord(1, "incident.opened"));
    });

    expect(
      await screen.findByTestId("incident-row-inc_2", {}, { timeout: 3000 }),
    ).toBeInTheDocument();
    expect(get.mock.calls.filter((c) => c[0] === INCIDENTS).length).toBeGreaterThan(before);
  });

  it("gives a verdict from a row and shows an existing verdict instead of the buttons", async () => {
    const user = userEvent.setup();
    stubStream();
    stubGet({
      [INCIDENTS]: () =>
        ok(
          pageOf([
            incident("inc_1"),
            incident("inc_2", {
              verdict: {
                incident_id: "inc_2",
                value: "not_an_issue",
                source: "user",
                check_versions: {},
                created: "2026-10-30T10:00:00Z",
              },
            }),
          ]),
        ),
    });
    const post = stubPost({ [VERDICT]: () => ok({}) });

    renderPage("/projects/prj_1/inbox");
    const row = await screen.findByTestId("incident-row-inc_1");
    await user.click(within(row).getByText("Give a verdict"));
    await user.click(within(row).getByRole("button", { name: "Real issue" }));

    await waitFor(() => {
      expect(bodyOf(post, VERDICT)).toEqual({ value: "real_issue" });
    });
    const judged = within(screen.getByTestId("incident-row-inc_2"));
    expect(judged.getByText("Not an issue")).toBeInTheDocument();
    expect(judged.queryByText("Give a verdict")).toBeNull();
  });

  it("loads more incidents with the offset", async () => {
    const user = userEvent.setup();
    stubStream();
    const get = stubGet({
      [INCIDENTS]: (init: never) =>
        (init as { params: { query: { offset: number } } }).params.query.offset === 0
          ? ok(pageOf([incident("inc_1")], true))
          : ok(pageOf([incident("inc_2")], false)),
    });

    renderPage("/projects/prj_1/inbox");
    await screen.findByTestId("incident-row-inc_1");
    await user.click(screen.getByRole("button", { name: "Load more incidents" }));

    expect(await screen.findByTestId("incident-row-inc_2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more incidents" })).toBeNull();
    const offsets = get.mock.calls
      .filter((c) => c[0] === INCIDENTS)
      .map((c) => (c[1] as { params: { query: { offset: number } } }).params.query.offset);
    expect(offsets).toEqual([0, 1]);
  });

  it("shows an empty state and an error with a retry", async () => {
    const user = userEvent.setup();
    stubStream();
    let fail = true;
    stubGet({
      [INCIDENTS]: () =>
        fail ? failure("boom", "Incidents are unavailable.", 500) : ok(pageOf([])),
    });

    renderPage("/projects/prj_1/inbox");
    expect(await screen.findByText("Incidents are unavailable.")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("No active incidents.")).toBeInTheDocument();
  });

  it("offers the miss report dialog on the Inbox", async () => {
    const user = userEvent.setup();
    stubStream();
    stubGet({ [INCIDENTS]: () => ok(pageOf([])) });

    renderPage("/projects/prj_1/inbox");
    await user.click(await screen.findByRole("button", { name: "Report a miss" }));

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });
});
