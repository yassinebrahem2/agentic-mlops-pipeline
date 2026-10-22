import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FolderBrowser } from "./FolderBrowser";

function renderFolderBrowser(props: {
  initialPath?: string;
  onChoose?: (path: string) => void;
  isSubmitting?: boolean;
}) {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={queryClient}>
      <FolderBrowser
        initialPath={props.initialPath}
        onChoose={props.onChoose ?? vi.fn()}
        isSubmitting={props.isSubmitting}
      />
    </QueryClientProvider>,
  );
}

function getUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

describe("FolderBrowser", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lists folders, marks git repository when is_git_repo, and enables Up button", async () => {
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = getUrl(input);
      if (url.includes("/api/folders")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              path: "/home/user",
              parent: "/home",
              roots: ["/"],
              entries: [
                { name: "project-a", path: "/home/user/project-a", is_git_repo: true },
                { name: "docs", path: "/home/user/docs", is_git_repo: false },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderFolderBrowser({});

    expect(await screen.findByText("/home/user")).toBeInTheDocument();
    expect(screen.getByText("project-a")).toBeInTheDocument();
    expect(screen.getByText("git repository")).toBeInTheDocument();
    expect(screen.getByText("docs")).toBeInTheDocument();

    const upBtn = screen.getByRole("button", { name: "Up" });
    expect(upBtn).toBeEnabled();
  });

  it("navigates up when Up button is clicked", async () => {
    const user = userEvent.setup();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = getUrl(input);
      if (url.includes("path=%2Fhome%2Fuser") || !url.includes("path=")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              path: "/home/user",
              parent: "/home",
              roots: ["/"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      if (url.includes("path=%2Fhome")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              path: "/home",
              parent: "/",
              roots: ["/"],
              entries: [{ name: "user", path: "/home/user", is_git_repo: false }],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(new Response(null, { status: 404 }));
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderFolderBrowser({});

    expect(await screen.findByText("/home/user")).toBeInTheDocument();
    const upBtn = screen.getByRole("button", { name: "Up" });
    await user.click(upBtn);

    expect(await screen.findByText("/home")).toBeInTheDocument();
  });

  it("opens a typed path via Folder path input and Go button", async () => {
    const user = userEvent.setup();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = getUrl(input);
      if (url.includes("/custom/path") || url.includes("%2Fcustom%2Fpath")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              path: "/custom/path",
              parent: "/custom",
              roots: ["/"],
              entries: [{ name: "code", path: "/custom/path/code", is_git_repo: true }],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({
            path: "/initial",
            parent: null,
            roots: ["/"],
            entries: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderFolderBrowser({});

    expect(await screen.findByText("/initial")).toBeInTheDocument();

    const input = screen.getByRole("textbox", { name: "Folder path" });
    await user.clear(input);
    await user.type(input, "/custom/path");

    const goBtn = screen.getByRole("button", { name: "Go" });
    await user.click(goBtn);

    expect(await screen.findByText("/custom/path")).toBeInTheDocument();
    expect(screen.getByText("code")).toBeInTheDocument();
  });

  it("opens subfolder when clicking on it", async () => {
    const user = userEvent.setup();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = getUrl(input);
      if (url.includes("path=%2Fhome%2Fsubfolder")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              path: "/home/subfolder",
              parent: "/home",
              roots: ["/"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({
            path: "/home",
            parent: null,
            roots: ["/"],
            entries: [{ name: "subfolder", path: "/home/subfolder", is_git_repo: false }],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderFolderBrowser({});

    expect(await screen.findByText("subfolder")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /subfolder/i }));

    expect(await screen.findByText("/home/subfolder")).toBeInTheDocument();
  });

  it("shows multiple drive roots on Windows and navigates when clicking one", async () => {
    const user = userEvent.setup();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = getUrl(input);
      if (url.includes("D%3A%5C") || url.includes("D:\\")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              path: "D:\\",
              parent: null,
              roots: ["C:\\", "D:\\"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({
            path: "C:\\Users",
            parent: "C:\\",
            roots: ["C:\\", "D:\\"],
            entries: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderFolderBrowser({});

    expect(await screen.findByText("C:\\Users")).toBeInTheDocument();
    const dDriveBtn = screen.getByRole("button", { name: "D:\\" });
    expect(dDriveBtn).toBeInTheDocument();

    await user.click(dDriveBtn);
    expect(await screen.findByText("Current folder:")).toHaveTextContent("D:\\");
  });

  it("shows a folder_not_found message and stays usable", async () => {
    const user = userEvent.setup();
    let queryCount = 0;
    const fakeFetch = vi.fn().mockImplementation(() => {
      queryCount++;
      if (queryCount === 1) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: "folder_not_found",
              message: "/missing/folder is not a folder on this machine.",
            }),
            { status: 404, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({
            path: "/recovered",
            parent: null,
            roots: ["/"],
            entries: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderFolderBrowser({ initialPath: "/missing/folder" });

    expect(
      await screen.findByText("/missing/folder is not a folder on this machine."),
    ).toBeInTheDocument();

    const input = screen.getByRole("textbox", { name: "Folder path" });
    expect(input).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, "/recovered");
    await user.click(screen.getByRole("button", { name: "Go" }));

    expect(await screen.findByText("/recovered")).toBeInTheDocument();
  });

  it("calls onChoose with current path when clicking 'Use this folder'", async () => {
    const user = userEvent.setup();
    const onChoose = vi.fn();
    const fakeFetch = vi.fn().mockImplementation(() => {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            path: "/chosen/folder",
            parent: "/chosen",
            roots: ["/"],
            entries: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderFolderBrowser({ onChoose });

    expect(await screen.findByText("/chosen/folder")).toBeInTheDocument();
    const chooseBtn = screen.getByRole("button", { name: "Use this folder" });
    await user.click(chooseBtn);

    expect(onChoose).toHaveBeenCalledWith("/chosen/folder");
  });

  it("disables 'Use this folder' button and shows progress text when isSubmitting is true", async () => {
    const fakeFetch = vi.fn().mockImplementation(() => {
      return Promise.resolve(
        new Response(
          JSON.stringify({
            path: "/chosen/folder",
            parent: "/chosen",
            roots: ["/"],
            entries: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderFolderBrowser({ isSubmitting: true });

    const submitBtn = await screen.findByRole("button", { name: /Adding project/i });
    expect(submitBtn).toBeDisabled();
    expect(submitBtn).toHaveTextContent("Adding project...");
  });
});
