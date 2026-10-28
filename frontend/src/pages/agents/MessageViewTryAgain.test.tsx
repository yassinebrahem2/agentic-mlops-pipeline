import { render, screen } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { describe, expect, it } from "vitest";
import { MessageView } from "./MessageView";
import type { MessageView as MessageViewType } from "./session/events";

describe("MessageView on a failed turn", () => {
  it("a failed agent message (error_kind keys_exhausted) shows no Try again", async () => {
    const failedAnswer: MessageViewType = {
      id: "a1",
      turn_id: "trn_1",
      author_kind: "agent",
      author_name: "orchestrator",
      from_agent: null,
      parts: [{ type: "text", text: "Partial answer" }],
      status: "complete",
      error_kind: "keys_exhausted",
      created_at: "2026-01-01T00:00:01Z",
    };
    // Rendered on a project's Agents page, where the project id is known.
    const router = createMemoryRouter(
      [
        {
          path: "/projects/:projectId/agents",
          element: <MessageView message={failedAnswer} sessionAgent="orchestrator" />,
        },
      ],
      { initialEntries: ["/projects/prj_1/agents"] },
    );
    render(<RouterProvider router={router} />);

    expect(await screen.findByText("Partial answer")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });
});
