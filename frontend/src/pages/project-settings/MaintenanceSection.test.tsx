import { QueryClient } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "@/api/schema";
import { MaintenanceSection } from "./MaintenanceSection";
import { expectationsKey } from "./expectationsKey";
import { failure, json, renderSection, stubEventStream, stubRoutes } from "./testing";

type Window = components["schemas"]["MaintenanceWindowView"];

const ROUTE = "/api/projects/prj_demo/maintenance-windows";

const BY_PERSON: Window = {
  id: "win_1",
  start: "2026-11-01T22:00:00Z",
  end: "2026-11-02T02:00:00Z",
  scope: ["daily_scores", "nightly_eval"],
  reason: "Warehouse migration",
  proposal_id: null,
  created_by: "user",
  created: "2026-10-31T09:00:00Z",
};
const BY_AGENT: Window = {
  ...BY_PERSON,
  id: "win_2",
  scope: ["psi_age"],
  reason: "Backfill running",
  proposal_id: "prop_77",
  created_by: "proposal",
};

let stream: ReturnType<typeof stubEventStream>;

beforeEach(() => {
  stream = stubEventStream();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function setup(options: { windows?: Window[]; routes?: Parameters<typeof stubRoutes>[0] } = {}) {
  let windows = options.windows ?? [BY_PERSON, BY_AGENT];
  const requests = stubRoutes({ [`GET ${ROUTE}`]: () => json(windows), ...options.routes });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const invalidated = vi.spyOn(client, "invalidateQueries");
  renderSection(<MaintenanceSection projectId="prj_demo" />, client);
  return {
    requests,
    invalidated,
    user: userEvent.setup(),
    setWindows: (next: Window[]) => {
      windows = next;
    },
  };
}

function fill(reason: string, scope: string) {
  fireEvent.change(screen.getByLabelText("Starts"), { target: { value: "2026-11-05T08:00" } });
  fireEvent.change(screen.getByLabelText("Ends"), { target: { value: "2026-11-05T10:00" } });
  fireEvent.change(screen.getByLabelText("Scope"), { target: { value: scope } });
  fireEvent.change(screen.getByLabelText("Reason"), { target: { value: reason } });
}

describe("MaintenanceSection", () => {
  it("lists windows with their scope and reason, and shows the proposal of an agent's", async () => {
    setup();

    const person = within(await screen.findByRole("listitem", { name: "Warehouse migration" }));
    expect(person.getByText("daily_scores")).toBeInTheDocument();
    expect(person.getByText("nightly_eval")).toBeInTheDocument();
    expect(person.getByText(/Created by user/)).toBeInTheDocument();
    expect(person.queryByText(/proposal/i)).toBeNull();
    const agent = within(screen.getByRole("listitem", { name: "Backfill running" }));
    expect(agent.getByText(/Proposed by an agent/)).toBeInTheDocument();
    expect(agent.queryByText(/Proposed by proposal/)).toBeNull();
    expect(agent.getByText(/prop_77/)).toBeInTheDocument();
  });

  it("says so when there are no windows", async () => {
    setup({ windows: [] });

    expect(await screen.findByText("No maintenance windows.")).toBeInTheDocument();
  });

  it("shows the server's error with a retry when the list cannot load", async () => {
    setup({ routes: { [`GET ${ROUTE}`]: () => failure("Windows unavailable", 500) } });

    expect(await screen.findByText("Windows unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("creates a window from the time range, scope and reason, and lists it", async () => {
    const created: Window = {
      ...BY_PERSON,
      id: "win_3",
      scope: ["a", "b"],
      reason: "Rotate keys",
    };
    const post = vi.fn<(body: unknown) => Response>(() => json(created, 201));
    const { user, setWindows } = setup({ windows: [], routes: { [`POST ${ROUTE}`]: post } });

    await screen.findByText("No maintenance windows.");
    expect(screen.getByRole("button", { name: "Add window" })).toBeDisabled();
    fill("Rotate keys", "a, b");
    setWindows([created]);
    await user.click(screen.getByRole("button", { name: "Add window" }));

    expect(await screen.findByRole("listitem", { name: "Rotate keys" })).toBeInTheDocument();
    expect(post).toHaveBeenCalledWith({
      start: new Date("2026-11-05T08:00").toISOString(),
      end: new Date("2026-11-05T10:00").toISOString(),
      scope: ["a", "b"],
      reason: "Rotate keys",
    });
    expect(screen.getByLabelText("Reason")).toHaveValue("");
  });

  it("shows a refused create as the server wrote it and keeps the form", async () => {
    const { user } = setup({
      windows: [],
      routes: { [`POST ${ROUTE}`]: () => failure("The window must end after it starts.", 422) },
    });

    await screen.findByText("No maintenance windows.");
    fill("Oops", "a");
    await user.click(screen.getByRole("button", { name: "Add window" }));

    expect(await screen.findByText("The window must end after it starts.")).toBeInTheDocument();
    expect(screen.getByLabelText("Reason")).toHaveValue("Oops");
  });

  it("deletes a window only after a confirmation, and it leaves the list", async () => {
    const del = vi.fn(() => new Response(null, { status: 204 }));
    const { user, setWindows } = setup({ routes: { [`DELETE ${ROUTE}/win_1`]: del } });

    const person = await screen.findByRole("listitem", { name: "Warehouse migration" });
    await user.click(
      within(person).getByRole("button", { name: "Delete window daily_scores, nightly_eval" }),
    );
    expect(del).not.toHaveBeenCalled();
    setWindows([BY_AGENT]);
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete window" }));

    await waitFor(() => {
      expect(screen.queryByRole("listitem", { name: "Warehouse migration" })).toBeNull();
    });
    expect(del).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("listitem", { name: "Backfill running" })).toBeInTheDocument();
  });

  it("shows a refused delete as the server wrote it", async () => {
    const { user } = setup({
      routes: { [`DELETE ${ROUTE}/win_1`]: () => failure("Window not found.", 404) },
    });

    const person = await screen.findByRole("listitem", { name: "Warehouse migration" });
    await user.click(
      within(person).getByRole("button", { name: "Delete window daily_scores, nightly_eval" }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete window" }));

    expect(await within(dialog).findByText("Window not found.")).toBeInTheDocument();
  });

  it("also refreshes the expectations after a window is created or deleted", async () => {
    const created: Window = { ...BY_PERSON, id: "win_3", reason: "Rotate keys" };
    const { user, invalidated } = setup({
      routes: {
        [`POST ${ROUTE}`]: () => json(created, 201),
        [`DELETE ${ROUTE}/win_1`]: () => new Response(null, { status: 204 }),
      },
    });
    const expectationKeyCalls = () =>
      invalidated.mock.calls.filter(
        (call) => JSON.stringify(call[0]?.queryKey) === JSON.stringify(expectationsKey("prj_demo")),
      ).length;

    await screen.findByRole("listitem", { name: "Warehouse migration" });
    fill("Rotate keys", "a");
    await user.click(screen.getByRole("button", { name: "Add window" }));
    await waitFor(() => {
      expect(expectationKeyCalls()).toBe(1);
    });
    await user.click(
      screen.getByRole("button", { name: "Delete window daily_scores, nightly_eval" }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete window" }));
    await waitFor(() => {
      expect(expectationKeyCalls()).toBe(2);
    });
  });

  it.each(["proposal.decided", "handoff.approved"] as const)(
    "refetches the windows when %s arrives",
    async (type) => {
      const { setWindows } = setup({ windows: [BY_PERSON] });
      await screen.findByRole("listitem", { name: "Warehouse migration" });

      setWindows([BY_PERSON, BY_AGENT]);
      await act(async () => {
        stream.emit(
          type,
          {
            id: "evt_1",
            seq: 1,
            ts: "2026-10-31T10:00:00Z",
            source: "proposals",
            subject_id: null,
            payload: {},
            type,
          },
          1,
        );
        await Promise.resolve();
      });

      expect(await screen.findByRole("listitem", { name: "Backfill running" })).toBeInTheDocument();
    },
  );
});
