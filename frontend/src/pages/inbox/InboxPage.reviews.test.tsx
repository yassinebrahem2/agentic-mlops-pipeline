import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import { project, stubProjects } from "@/test/projects";
import { renderPage } from "@/test/render";
import { projectStub, sampleReviews, TestFakeStream } from "./testing";

describe("InboxPage local reviews", () => {
  let fakeStream: TestFakeStream;
  let getSpy: MockInstance | undefined;
  let postSpy: MockInstance | undefined;
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
    postSpy?.mockRestore();
    openStreamSpy?.mockRestore();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders changes to merge with agent, task title, files changed, and diff", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({
          data: sampleReviews,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Changes to merge")).toBeInTheDocument();
    expect(await screen.findByText("1 waiting")).toBeInTheDocument();
    expect(screen.getByText("Add user profile endpoint")).toBeInTheDocument();
    expect(screen.getByText("These files in your folder will change:")).toBeInTheDocument();
    expect(screen.getByText("src/users/profile.py")).toBeInTheDocument();
    expect(screen.getByText("tests/test_profile.py")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Diff view" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();

    // Sidebar badge counts 1 from review
    const sidebar = within(screen.getByRole("complementary"));
    const inboxLink = sidebar.getByRole("link", { name: /Inbox/ });
    await waitFor(() => {
      expect(within(inboxLink).getByText("1")).toBeInTheDocument();
    });
  });

  it("approving a local review calls the decision endpoint", async () => {
    const user = userEvent.setup();

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({
          data: sampleReviews,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    const firstReview = sampleReviews[0];
    postSpy = vi.spyOn(api, "POST").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/reviews/{task_id}/decision") {
        return Promise.resolve({
          data: { ...(firstReview ? firstReview.task : {}), status: "merged" },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected POST ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Add user profile endpoint")).toBeInTheDocument();
    const approveBtn = screen.getByRole("button", { name: "Approve" });
    await user.click(approveBtn);

    expect(postSpy).toHaveBeenCalledWith("/api/projects/{project_id}/reviews/{task_id}/decision", {
      params: {
        path: {
          project_id: "prj_1",
          task_id: "tsk_1",
        },
      },
      body: {
        approve: true,
        note: null,
      },
    });
  });

  it("rejecting a local review with a note calls the decision endpoint", async () => {
    const user = userEvent.setup();

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({
          data: sampleReviews,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    const firstReview = sampleReviews[0];
    postSpy = vi.spyOn(api, "POST").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/reviews/{task_id}/decision") {
        return Promise.resolve({
          data: { ...(firstReview ? firstReview.task : {}), status: "closed" },
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected POST ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Add user profile endpoint")).toBeInTheDocument();

    const noteInput = screen.getByPlaceholderText("Optional note for rejection...");
    await user.type(noteInput, "Please add more unit tests");

    const rejectBtn = screen.getByRole("button", { name: "Reject" });
    await user.click(rejectBtn);

    expect(postSpy).toHaveBeenCalledWith("/api/projects/{project_id}/reviews/{task_id}/decision", {
      params: {
        path: {
          project_id: "prj_1",
          task_id: "tsk_1",
        },
      },
      body: {
        approve: false,
        note: "Please add more unit tests",
      },
    });
  });

  it("shows 409 conflict error message on review decision", async () => {
    const user = userEvent.setup();

    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({
          data: sampleReviews,
          response: new Response(null, { status: 200 }),
        });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    postSpy = vi.spyOn(api, "POST").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/reviews/{task_id}/decision") {
        return Promise.resolve({
          error: {
            code: "conflict",
            message: "Cannot merge: conflicting changes in src/users/profile.py",
          },
          response: new Response(null, { status: 409 }),
        });
      }
      return Promise.reject(new Error(`Unexpected POST ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Add user profile endpoint")).toBeInTheDocument();
    const approveBtn = screen.getByRole("button", { name: "Approve" });
    await user.click(approveBtn);

    expect(
      await screen.findByText("Cannot merge: conflicting changes in src/users/profile.py"),
    ).toBeInTheDocument();
  });

  it("shows error alert when reviews fetch fails", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.reject(new ApiError("internal", "Database disk full", 500));
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}") {
        return Promise.resolve({
          data: projectStub,
          response: new Response(null, { status: 200 }),
        });
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    });

    renderPage("/projects/prj_1/inbox");

    expect(await screen.findByText("Error loading local reviews")).toBeInTheDocument();
    expect(screen.getByText("Database disk full")).toBeInTheDocument();
  });
});
