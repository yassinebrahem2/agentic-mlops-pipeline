import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { TaskBar, type TaskView } from "./TaskBar";

describe("TaskBar", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  const baseTask: TaskView = {
    id: "tsk_123",
    title: "Implement user authentication",
    branch: "feat/auth",
    status: "active",
    review_kind: "github",
    pr_number: 42,
    pr_url: "https://github.com/example/repo/pull/42",
  };

  it("renders with PR link and number", () => {
    render(<TaskBar task={baseTask} projectId="prj_1" />);

    expect(screen.getByText("Implement user authentication")).toBeInTheDocument();
    expect(screen.getByText("feat/auth")).toBeInTheDocument();

    const prLink = screen.getByRole("link", { name: "PR #42" });
    expect(prLink).toHaveAttribute("href", "https://github.com/example/repo/pull/42");
    expect(prLink).toHaveAttribute("target", "_blank");
    expect(screen.queryByText("local review")).not.toBeInTheDocument();
  });

  it("renders with 'local review' when there is no PR link", () => {
    const localTask: TaskView = {
      ...baseTask,
      review_kind: "local",
      pr_number: null,
      pr_url: null,
    };

    render(<TaskBar task={localTask} projectId="prj_1" />);

    expect(screen.getByText("Implement user authentication")).toBeInTheDocument();
    expect(screen.getByText("feat/auth")).toBeInTheDocument();
    expect(screen.getByText("local review")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("opens confirmation dialog and allows cancelling", async () => {
    const user = userEvent.setup();
    const postSpy = vi.spyOn(api, "POST");

    render(<TaskBar task={baseTask} projectId="prj_1" />);

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Discard task" }));

    const dialog = screen.getByRole("dialog", { name: "Discard task" });
    expect(dialog).toBeInTheDocument();
    expect(screen.getByText(/Are you sure you want to discard this task\?/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(postSpy).not.toHaveBeenCalled();
  });

  it("discards task after confirmation and calls onDiscard callback", async () => {
    const user = userEvent.setup();
    const onDiscard = vi.fn();
    const postSpy = vi.spyOn(api, "POST").mockResolvedValue({
      data: undefined,
      response: new Response(null, { status: 204 }),
    });

    render(<TaskBar task={baseTask} projectId="prj_1" onDiscard={onDiscard} />);

    await user.click(screen.getByRole("button", { name: "Discard task" }));
    expect(screen.getByRole("dialog", { name: "Discard task" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Discard" }));

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith(
        "/api/projects/{project_id}/tasks/{task_id}/discard",
        expect.objectContaining({
          params: { path: { project_id: "prj_1", task_id: "tsk_123" } },
        }),
      );
    });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(onDiscard).toHaveBeenCalledTimes(1);
  });

  it("shows 409 error message when discarding is rejected", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "POST").mockRejectedValue(
      new ApiError("task_in_progress", "Cannot discard a task with active workers.", 409),
    );

    render(<TaskBar task={baseTask} projectId="prj_1" />);

    await user.click(screen.getByRole("button", { name: "Discard task" }));
    await user.click(screen.getByRole("button", { name: "Discard" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Cannot discard a task with active workers.");
    expect(screen.getByRole("dialog", { name: "Discard task" })).toBeInTheDocument();
  });
});
