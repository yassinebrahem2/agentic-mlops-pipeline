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

type ActivityPageBody = components["schemas"]["ActivityPage"];
type Entry = ActivityPageBody["entries"][number];

function eventEntry(
  id: string,
  seq: number,
  overrides: Partial<components["schemas"]["EventRecord"]> = {},
  incidentId: string | null = null,
): Entry {
  const ts = `2026-10-30T10:00:${String(seq).padStart(2, "0")}Z`;
  return {
    kind: "event",
    ts,
    incident_id: incidentId,
    event: {
      id,
      seq,
      ts,
      source: "orchestrator",
      type: "message.sent",
      subject_id: incidentId,
      payload: { text: "<b>hello</b>" },
      ...overrides,
    },
  };
}

function auditEntry(id: string, sec: number): Entry {
  const ts = `2026-10-30T10:00:${String(sec).padStart(2, "0")}Z`;
  return {
    kind: "audit",
    ts,
    audit: {
      id,
      ts,
      actor: "coder",
      action: "Write",
      risk_class: "write_code",
      permission_level: "ask",
      decision: "approved",
      target: "src/main.py",
      detail: { path: "src/main.py" },
    },
  };
}

function ok(data: ActivityPageBody) {
  return Promise.resolve({ data, response: new Response(null, { status: 200 }) });
}

describe("ActivityPage", () => {
  let fakeStream: TestFakeStream;
  let getSpy: MockInstance | undefined;
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
    openStreamSpy?.mockRestore();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubActivity(handler: (query: Record<string, unknown>) => Promise<unknown>) {
    getSpy = vi.spyOn(api, "GET").mockImplementation(((
      path: string,
      init?: { params?: { query?: Record<string, unknown> } },
    ) => {
      if (path === "/api/projects/{project_id}/activity") {
        return handler(init?.params?.query ?? {});
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: project("prj_1", "Test Project", null),
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    }) as never);
  }

  function activityQueries(): Record<string, unknown>[] {
    return (getSpy?.mock.calls ?? [])
      .filter((c) => c[0] === "/api/projects/{project_id}/activity")
      .map((c) => (c[1] as { params: { query: Record<string, unknown> } }).params.query);
  }

  it("is a project nav entry named Activity at order 50", () => {
    expect(route.path).toBe("/projects/:projectId/activity");
    expect(route.nav).toMatchObject({ area: "project", label: "Activity", order: 50 });
  });

  it("renders events and audit records, payloads as text, and links an incident entry", async () => {
    stubActivity(() =>
      ok({
        entries: [
          auditEntry("aud_1", 5),
          eventEntry("evt_2", 2, { type: "incident.opened" }, "inc_01ABC"),
          eventEntry("evt_1", 1),
        ],
        next_before: null,
      }),
    );

    renderPage("/projects/prj_1/activity");

    const audit = await screen.findByTestId("activity-audit-aud_1");
    expect(within(audit).getByText("coder")).toBeInTheDocument();
    expect(within(audit).getByText("Write")).toBeInTheDocument();
    expect(within(audit).getByText("src/main.py")).toBeInTheDocument();
    expect(within(audit).getByText("approved")).toBeInTheDocument();

    const incident = screen.getByTestId("activity-event-evt_2");
    expect(within(incident).getByText("incident.opened")).toBeInTheDocument();
    expect(within(incident).getByRole("link", { name: "inc_01ABC" })).toHaveAttribute(
      "href",
      "/projects/prj_1/incidents/inc_01ABC",
    );

    const plain = screen.getByTestId("activity-event-evt_1");
    expect(within(plain).queryByRole("link")).toBeNull();
    // The payload is shown as text, never parsed as HTML.
    expect(within(plain).getByText(/<b>hello<\/b>/)).toBeInTheDocument();
    expect(plain.querySelector("b")).toBeNull();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("filters by event type and by agent", async () => {
    const user = userEvent.setup();
    stubActivity(() => ok({ entries: [eventEntry("evt_1", 1)], next_before: null }));
    renderPage("/projects/prj_1/activity");
    await screen.findByTestId("activity-event-evt_1");

    await user.click(screen.getByRole("combobox", { name: "Event type" }));
    await user.click(await screen.findByRole("option", { name: "approval.requested" }));
    await waitFor(() => {
      expect(activityQueries()).toContainEqual(
        expect.objectContaining({ type: "approval.requested" }),
      );
    });

    await user.type(screen.getByLabelText("Agent"), "coder");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => {
      expect(activityQueries()).toContainEqual(
        expect.objectContaining({ type: "approval.requested", source: "coder" }),
      );
    });
  });

  it("loads the next page with the cursor and hides the button at the end", async () => {
    const user = userEvent.setup();
    stubActivity((query) =>
      query.before === "CURSOR_1"
        ? ok({ entries: [eventEntry("evt_1", 1)], next_before: null })
        : ok({ entries: [eventEntry("evt_2", 2)], next_before: "CURSOR_1" }),
    );
    renderPage("/projects/prj_1/activity");
    await screen.findByTestId("activity-event-evt_2");

    await user.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByTestId("activity-event-evt_1")).toBeInTheDocument();
    expect(screen.getByTestId("activity-event-evt_2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("shows an empty state", async () => {
    stubActivity(() => ok({ entries: [], next_before: null }));
    renderPage("/projects/prj_1/activity");
    expect(await screen.findByText("No activity yet.")).toBeInTheDocument();
  });

  it("shows the server's error with a retry that reloads", async () => {
    const user = userEvent.setup();
    let calls = 0;
    stubActivity(() => {
      calls += 1;
      if (calls === 1) {
        return Promise.reject(new ApiError("invalid_cursor", "The page cursor is not valid", 422));
      }
      return ok({ entries: [eventEntry("evt_1", 1)], next_before: null });
    });
    renderPage("/projects/prj_1/activity");

    expect(await screen.findByText(/The page cursor is not valid/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByTestId("activity-event-evt_1")).toBeInTheDocument();
  });

  it("refreshes when a project event arrives, without repeating an entry", async () => {
    let calls = 0;
    stubActivity(() => {
      calls += 1;
      return ok({
        entries:
          calls === 1 ? [eventEntry("evt_1", 1)] : [eventEntry("evt_2", 2), eventEntry("evt_1", 1)],
        next_before: null,
      });
    });
    renderPage("/projects/prj_1/activity");
    await screen.findByTestId("activity-event-evt_1");

    act(() => {
      fakeStream.emitOpen();
      fakeStream.emit(
        "note.saved",
        {
          id: "evt_2",
          seq: 2,
          ts: "2026-10-30T10:00:02Z",
          source: "user",
          type: "note.saved",
          subject_id: null,
          payload: {},
        },
        2,
      );
    });

    expect(await screen.findByTestId("activity-event-evt_2")).toBeInTheDocument();
    expect(screen.getAllByTestId("activity-event-evt_1")).toHaveLength(1);
  });
});
