import { screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import { project, stubProjects } from "@/test/projects";
import { renderPage } from "@/test/render";
import { projectStub, sampleActivity, TestFakeStream } from "./testing";

describe("InboxPage GitHub activity", () => {
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

  it("renders pull requests and issues activity with external links", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.resolve({
          data: sampleActivity,
          response: new Response(null, { status: 200 }),
        });
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

    expect(await screen.findByText("Pull requests and issues")).toBeInTheDocument();
    expect(await screen.findByText("2 total")).toBeInTheDocument();

    const prItem = await screen.findByTestId("activity-item-pr-42");
    expect(within(prItem).getByText("#42 feat: add analytics pipeline")).toBeInTheDocument();
    expect(within(prItem).getByText("orchestrator")).toBeInTheDocument();
    expect(within(prItem).getByText("open")).toBeInTheDocument();
    const prLink = within(prItem).getByRole("link");
    expect(prLink).toHaveAttribute("href", "https://github.com/org/repo/pull/42");
    expect(prLink).toHaveAttribute("target", "_blank");

    const issueItem = screen.getByTestId("activity-item-issue-7");
    expect(within(issueItem).getByText("#7 bug: fix memory leak")).toBeInTheDocument();
    expect(within(issueItem).getByText("debugger")).toBeInTheDocument();
    expect(within(issueItem).getByText("open")).toBeInTheDocument();
    const issueLink = within(issueItem).getByRole("link");
    expect(issueLink).toHaveAttribute("href", "https://github.com/org/repo/issues/7");
    expect(issueLink).toHaveAttribute("target", "_blank");
  });

  it("shows error alert when activity fetch fails", async () => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((path: string) => {
      if (path === "/api/projects/{project_id}/approvals") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/reviews") {
        return Promise.resolve({ data: [], response: new Response(null, { status: 200 }) });
      }
      if (path === "/api/projects/{project_id}/github/activity") {
        return Promise.reject(new ApiError("internal", "GitHub API rate limited", 500));
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

    expect(await screen.findByText("Error loading activity")).toBeInTheDocument();
    expect(screen.getByText("GitHub API rate limited")).toBeInTheDocument();
  });
});
