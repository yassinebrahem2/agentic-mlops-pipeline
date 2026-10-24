import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { getUrl, json, renderFolderBrowser } from "./FolderBrowser.testing";

describe("FolderBrowserInput", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows a folder_not_found message and stays usable", async () => {
    const user = userEvent.setup();
    let queryCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => {
        queryCount++;
        return Promise.resolve(
          queryCount === 1
            ? json(
                {
                  code: "folder_not_found",
                  message: "/missing/folder is not a folder on this machine.",
                },
                404,
              )
            : json({ path: "/recovered", parent: null, roots: ["/"], entries: [] }),
        );
      }),
    );

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

  it("keeps a path typed while the first listing is still loading when the listing arrives", async () => {
    const user = userEvent.setup();
    let resolveListing!: (value: Response) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        () =>
          new Promise<Response>((resolve) => {
            resolveListing = resolve;
          }),
      ),
    );

    renderFolderBrowser({});

    const input = screen.getByRole("textbox", { name: "Folder path" });
    await user.type(input, "/typed/before/loaded");
    expect(input).toHaveValue("/typed/before/loaded");

    resolveListing(json({ path: "/home/user", parent: "/home", roots: ["/"], entries: [] }));

    expect(await screen.findByText("/home/user")).toBeInTheDocument();
    expect(input).toHaveValue("/typed/before/loaded");
  });

  it("shows the new listing path in the box after Go, a folder click, Up, or a root button", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const parsed = new URL(getUrl(input), "http://localhost");
        const pathParam = parsed.searchParams.get("path");
        if (pathParam === "C:\\") {
          return Promise.resolve(
            json({ path: "C:\\", parent: null, roots: ["C:\\", "D:\\"], entries: [] }),
          );
        }
        if (pathParam === "/a/b/sub") {
          return Promise.resolve(
            json({ path: "/a/b/sub", parent: "/a/b", roots: ["/"], entries: [] }),
          );
        }
        if (pathParam === "/a/b" || pathParam === "/a/b/") {
          return Promise.resolve(
            json({
              path: "/a/b",
              parent: "/a",
              roots: ["/"],
              entries: [{ name: "sub", path: "/a/b/sub", is_git_repo: false }],
            }),
          );
        }
        if (pathParam === "/a") {
          return Promise.resolve(
            json({
              path: "/a",
              parent: null,
              roots: ["C:\\", "D:\\"],
              entries: [{ name: "b", path: "/a/b", is_git_repo: false }],
            }),
          );
        }
        return Promise.resolve(
          json({
            path: "/initial",
            parent: null,
            roots: ["C:\\", "D:\\"],
            entries: [{ name: "b", path: "/a/b", is_git_repo: false }],
          }),
        );
      }),
    );

    renderFolderBrowser({});
    const input = screen.getByRole("textbox", { name: "Folder path" });
    expect(await screen.findByText("/initial")).toBeInTheDocument();
    expect(input).toHaveValue("/initial");

    // 1. Go button: type a path differing from the canonical returned path ("/a/b/" vs "/a/b")
    await user.clear(input);
    await user.type(input, "/a/b/");
    expect(input).toHaveValue("/a/b/");
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
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((input: RequestInfo | URL) => {
        const isMissing = getUrl(input).includes("nonexistent");
        return Promise.resolve(
          isMissing
            ? json(
                {
                  code: "folder_not_found",
                  message: "/nonexistent is not a folder on this machine.",
                },
                404,
              )
            : json({ path: "/initial", parent: null, roots: ["/"], entries: [] }),
        );
      }),
    );

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
