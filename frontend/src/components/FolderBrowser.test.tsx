import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getUrl, json, renderFolderBrowser } from "./FolderBrowser.testing";

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
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        if (getUrl(input).includes("/api/folders")) {
          return Promise.resolve(
            json({
              path: "/home/user",
              parent: "/home",
              roots: ["/"],
              entries: [
                { name: "project-a", path: "/home/user/project-a", is_git_repo: true },
                { name: "docs", path: "/home/user/docs", is_git_repo: false },
              ],
            }),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      }),
    );

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
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const url = getUrl(input);
        if (url.includes("path=%2Fhome%2Fuser") || !url.includes("path=")) {
          return Promise.resolve(
            json({ path: "/home/user", parent: "/home", roots: ["/"], entries: [] }),
          );
        }
        if (url.includes("path=%2Fhome")) {
          return Promise.resolve(
            json({
              path: "/home",
              parent: "/",
              roots: ["/"],
              entries: [{ name: "user", path: "/home/user", is_git_repo: false }],
            }),
          );
        }
        return Promise.resolve(new Response(null, { status: 404 }));
      }),
    );

    renderFolderBrowser({});

    expect(await screen.findByText("/home/user")).toBeInTheDocument();
    const upBtn = screen.getByRole("button", { name: "Up" });
    await user.click(upBtn);

    expect(await screen.findByText("/home")).toBeInTheDocument();
  });

  it("opens a typed path via Folder path input and Go button", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const url = getUrl(input);
        if (url.includes("/custom/path") || url.includes("%2Fcustom%2Fpath")) {
          return Promise.resolve(
            json({
              path: "/custom/path",
              parent: "/custom",
              roots: ["/"],
              entries: [{ name: "code", path: "/custom/path/code", is_git_repo: true }],
            }),
          );
        }
        return Promise.resolve(json({ path: "/initial", parent: null, roots: ["/"], entries: [] }));
      }),
    );

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
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const isSub = getUrl(input).includes("path=%2Fhome%2Fsubfolder");
        return Promise.resolve(
          json(
            isSub
              ? { path: "/home/subfolder", parent: "/home", roots: ["/"], entries: [] }
              : {
                  path: "/home",
                  parent: null,
                  roots: ["/"],
                  entries: [{ name: "subfolder", path: "/home/subfolder", is_git_repo: false }],
                },
          ),
        );
      }),
    );

    renderFolderBrowser({});

    expect(await screen.findByText("subfolder")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /subfolder/i }));

    expect(await screen.findByText("/home/subfolder")).toBeInTheDocument();
  });

  it("shows multiple drive roots on Windows and navigates when clicking one", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const isD = getUrl(input).includes("D%3A%5C") || getUrl(input).includes("D:\\");
        return Promise.resolve(
          json(
            isD
              ? { path: "D:\\", parent: null, roots: ["C:\\", "D:\\"], entries: [] }
              : { path: "C:\\Users", parent: "C:\\", roots: ["C:\\", "D:\\"], entries: [] },
          ),
        );
      }),
    );

    renderFolderBrowser({});

    expect(await screen.findByText("C:\\Users")).toBeInTheDocument();
    const dDriveBtn = screen.getByRole("button", { name: "D:\\" });
    expect(dDriveBtn).toBeInTheDocument();

    await user.click(dDriveBtn);
    expect(await screen.findByText("Current folder:")).toHaveTextContent("D:\\");
  });

  it("calls onChoose with current path when clicking 'Use this folder'", async () => {
    const user = userEvent.setup();
    const onChoose = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          json({ path: "/chosen/folder", parent: "/chosen", roots: ["/"], entries: [] }),
        ),
    );

    renderFolderBrowser({ onChoose });

    expect(await screen.findByText("/chosen/folder")).toBeInTheDocument();
    const chooseBtn = screen.getByRole("button", { name: "Use this folder" });
    await user.click(chooseBtn);

    expect(onChoose).toHaveBeenCalledWith("/chosen/folder");
  });

  it("disables button and shows custom busyLabel or default when isSubmitting is true", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          json({ path: "/chosen/folder", parent: "/chosen", roots: ["/"], entries: [] }),
        ),
    );

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
});
