import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FolderBrowser } from "./FolderBrowser";

function renderFolderBrowser(props: {
  initialPath?: string;
  onChoose?: (path: string) => void;
  isSubmitting?: boolean;
  busyLabel?: string;
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
        busyLabel={props.busyLabel}
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

  it("disables button and shows custom busyLabel or default when isSubmitting is true", async () => {
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

    const { unmount } = renderFolderBrowser({ isSubmitting: true, busyLabel: "Working on it..." });

    const submitBtn = await screen.findByRole("button", { name: /Working on it/i });
    expect(submitBtn).toBeDisabled();
    expect(submitBtn).toHaveTextContent("Working on it...");

    unmount();
    renderFolderBrowser({ isSubmitting: true });
    const defaultBtn = await screen.findByRole("button", { name: "Use this folder" });
    expect(defaultBtn).toBeDisabled();
    expect(defaultBtn).toHaveTextContent("Use this folder");
  });

  it("keeps a path typed while the first listing is still loading when the listing arrives", async () => {
    const user = userEvent.setup();
    let resolveListing!: (value: Response) => void;
    const listingPromise = new Promise<Response>((resolve) => {
      resolveListing = resolve;
    });

    const fakeFetch = vi.fn().mockImplementation(() => listingPromise);
    vi.stubGlobal("fetch", fakeFetch);

    renderFolderBrowser({});

    const input = screen.getByRole("textbox", { name: "Folder path" });
    await user.type(input, "/typed/before/loaded");
    expect(input).toHaveValue("/typed/before/loaded");

    resolveListing(
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

    expect(await screen.findByText("/home/user")).toBeInTheDocument();
    expect(input).toHaveValue("/typed/before/loaded");
  });

  it("shows the new listing path in the box after Go, a folder click, Up, or a root button", async () => {
    const user = userEvent.setup();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const parsed = new URL(getUrl(input), "http://localhost");
      const pathParam = parsed.searchParams.get("path");
      if (pathParam === "C:\\") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              path: "C:\\",
              parent: null,
              roots: ["C:\\", "D:\\"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      if (pathParam === "/a/b/sub") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              path: "/a/b/sub",
              parent: "/a/b",
              roots: ["/"],
              entries: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      if (pathParam === "/a/b") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              path: "/a/b",
              parent: "/a",
              roots: ["/"],
              entries: [{ name: "sub", path: "/a/b/sub", is_git_repo: false }],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }
      if (pathParam === "/a") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              path: "/a",
              parent: null,
              roots: ["C:\\", "D:\\"],
              entries: [{ name: "b", path: "/a/b", is_git_repo: false }],
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
            roots: ["C:\\", "D:\\"],
            entries: [{ name: "b", path: "/a/b", is_git_repo: false }],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    vi.stubGlobal("fetch", fakeFetch);

    renderFolderBrowser({});
    const input = screen.getByRole("textbox", { name: "Folder path" });
    expect(await screen.findByText("/initial")).toBeInTheDocument();
    expect(input).toHaveValue("/initial");

    // 1. Go button
    await user.clear(input);
    await user.type(input, "/a/b");
    await user.click(screen.getByRole("button", { name: "Go" }));
    expect(await screen.findByText("/a/b")).toBeInTheDocument();
    expect(input).toHaveValue("/a/b");

    // 2. Folder click: type something first so isEdited is true, then click folder
    await user.type(input, "-edited");
    expect(input).toHaveValue("/a/b-edited");
    const subFolderBtn = await screen.findByRole("button", { name: /sub/i });
    await user.click(subFolderBtn);
    expect(await screen.findByText("/a/b/sub")).toBeInTheDocument();
    expect(input).toHaveValue("/a/b/sub");

    // 3. Up button: type something first, then click Up
    await user.type(input, "-edited");
    expect(input).toHaveValue("/a/b/sub-edited");
    const upBtn = screen.getByRole("button", { name: "Up" });
    await user.click(upBtn);
    expect(await screen.findByText("/a/b")).toBeInTheDocument();
    expect(input).toHaveValue("/a/b");

    // 4. Root button: type something first, navigate to /a where roots are displayed
    const upBtn2 = screen.getByRole("button", { name: "Up" });
    await user.click(upBtn2);
    expect(await screen.findByText("/a")).toBeInTheDocument();
    await user.type(input, "-edited");
    expect(input).toHaveValue("/a-edited");
    const cDriveBtn = screen.getByRole("button", { name: "C:\\" });
    await user.click(cDriveBtn);
    expect(await screen.findByText("Current folder:")).toHaveTextContent("C:\\");
    expect(input).toHaveValue("C:\\");
  });

  it("keeps typed text in the box and shows the error when Go is clicked with a nonexistent path", async () => {
    const user = userEvent.setup();
    const fakeFetch = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = getUrl(input);
      if (url.includes("nonexistent")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: "folder_not_found",
              message: "/nonexistent is not a folder on this machine.",
            }),
            { status: 404, headers: { "Content-Type": "application/json" } },
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
    await user.type(input, "/nonexistent");
    await user.click(screen.getByRole("button", { name: "Go" }));

    expect(
      await screen.findByText("/nonexistent is not a folder on this machine."),
    ).toBeInTheDocument();
    expect(input).toHaveValue("/nonexistent");
  });
});
