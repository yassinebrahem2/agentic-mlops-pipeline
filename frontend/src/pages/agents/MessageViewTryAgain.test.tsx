import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { MessageView } from "./MessageView";
import type { MessageView as MessageViewType } from "./session/events";

const retryRoute = "/api/projects/{project_id}/agents/{agent}/turns/{turn_id}/retry";

// Messages show on a project's Agents page; Try again needs that project.
function renderInProject(element: ReactElement) {
  const router = createMemoryRouter([{ path: "/projects/:projectId/agents", element }], {
    initialEntries: ["/projects/prj_1/agents"],
  });
  return render(<RouterProvider router={router} />);
}

const failedAnswer = (errorKind: MessageViewType["error_kind"]): MessageViewType => ({
  id: "a1",
  turn_id: "trn_1",
  author_kind: "agent",
  author_name: "orchestrator",
  from_agent: null,
  parts: [{ type: "text", text: "Partial answer" }],
  // A failed turn's answer is complete: the failure is in error_kind, never in the status.
  status: "complete",
  error_kind: errorKind,
  created_at: "2026-01-01T00:00:01Z",
});

describe("MessageView Try again on a failed turn", () => {
  let postSpy: MockInstance;

  beforeEach(() => {
    postSpy = vi
      .spyOn(api, "POST")
      .mockImplementation(() =>
        Promise.resolve({ data: undefined, response: new Response(null, { status: 204 }) }),
      );
  });

  afterEach(() => {
    postSpy.mockRestore();
  });

  it("re-runs the turn through the retry route when its keys were exhausted", async () => {
    const user = userEvent.setup();
    renderInProject(
      <MessageView message={failedAnswer("keys_exhausted")} sessionAgent="orchestrator" />,
    );

    await user.click(await screen.findByRole("button", { name: "Try again" }));

    expect(postSpy).toHaveBeenCalledExactlyOnceWith(retryRoute, {
      params: { path: { project_id: "prj_1", agent: "orchestrator", turn_id: "trn_1" } },
    });
  });

  it("disables Try again while the retry is pending and shows its error", async () => {
    let answer: ((response: Response) => void) | undefined;
    postSpy.mockImplementation(
      () =>
        new Promise((done) => {
          answer = (response) => {
            done({
              error: {
                code: "turn_not_retryable",
                message: "Only the last turn can be tried again.",
              },
              response,
            });
          };
        }),
    );
    const user = userEvent.setup();
    renderInProject(
      <MessageView message={failedAnswer("keys_exhausted")} sessionAgent="orchestrator" />,
    );

    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(screen.getByRole("button", { name: "Try again" })).toBeDisabled();

    answer?.(new Response(null, { status: 409 }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Only the last turn can be tried again.",
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });

  it("offers no Try again for another failure", async () => {
    renderInProject(<MessageView message={failedAnswer(null)} sessionAgent="orchestrator" />);

    expect(await screen.findByText("Partial answer")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });
});
