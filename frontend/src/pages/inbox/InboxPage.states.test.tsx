import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError, errorText } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import { FakeStream } from "@/pages/agents/session/testing";
import { InboxPage } from "./InboxPage";

const APPROVALS = "/api/projects/{project_id}/approvals";
const REVIEWS = "/api/projects/{project_id}/reviews";
const ACTIVITY = "/api/projects/{project_id}/github/activity";

function renderInbox(path: string, routePath: string) {
  const router = createMemoryRouter([{ path: routePath, element: <InboxPage /> }], {
    initialEntries: [path],
  });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

function stubGet(failing: string, error: Error): MockInstance {
  return vi
    .spyOn(api, "GET")
    .mockImplementation((path: string) =>
      path === failing
        ? Promise.reject(error)
        : Promise.resolve({ data: [], response: new Response(null, { status: 200 }) }),
    );
}

describe("InboxPage states", () => {
  let openStreamSpy: MockInstance | undefined;

  beforeEach(() => {
    openStreamSpy = vi
      .spyOn(streamModule, "openStream")
      .mockImplementation((url: string) => new FakeStream(url));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders no section and calls no project route while no project is selected", () => {
    const getSpy = vi.spyOn(api, "GET");

    renderInbox("/inbox", "/inbox");

    expect(screen.queryByRole("heading", { name: "Pending approvals" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Changes to merge" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Pull requests and issues" }),
    ).not.toBeInTheDocument();
    expect(getSpy).not.toHaveBeenCalled();
    expect(openStreamSpy).not.toHaveBeenCalled();
  });

  it.each([
    ["approvals", APPROVALS, "Error loading approvals"],
    ["local reviews", REVIEWS, "Error loading local reviews"],
    ["activity", ACTIVITY, "Error loading activity"],
  ])(
    "shows the shared error text, not a raw message, when loading %s fails unexpectedly",
    async (_name, failing, title) => {
      const unexpected = new Error("socket hang up");
      stubGet(failing, unexpected);

      renderInbox("/projects/prj_1/inbox", "/projects/:projectId/inbox");

      expect(await screen.findByText(title)).toBeInTheDocument();
      expect(screen.getByText(errorText(unexpected))).toBeInTheDocument();
      expect(screen.queryByText("socket hang up")).not.toBeInTheDocument();
    },
  );

  it("shows the server's message when loading fails with an API error", async () => {
    stubGet(REVIEWS, new ApiError("internal", "Database disk full", 500));

    renderInbox("/projects/prj_1/inbox", "/projects/:projectId/inbox");

    expect(await screen.findByText("Database disk full")).toBeInTheDocument();
  });
});
