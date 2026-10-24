import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { PageRoute } from "@/app/routes";
import { resetCurrentProjectStore } from "@/lib/useCurrentProject";
import { project, stubProjects } from "@/test/projects";
import { renderPage } from "@/test/render";

const routes: PageRoute[] = [
  { path: "/", element: <div>Home</div>, nav: { area: "app", label: "Projects", order: 0 } },
  {
    path: "/providers",
    element: <div>Providers Page</div>,
    nav: { area: "app", label: "AI providers", order: 10 },
  },
  {
    path: "/projects/:projectId/agents",
    element: <div>Agents Page</div>,
    nav: { area: "project", label: "Agents", order: 1 },
  },
];

// The shell renders the sidebar twice (narrow dialog and wide column); the wide one is always mounted.
function sidebar() {
  return within(screen.getByRole("complementary"));
}

describe("Sidebar current project", () => {
  beforeEach(() => {
    resetCurrentProjectStore();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("keeps the last opened project on an app page", async () => {
    stubProjects([
      project("prj_a", "Alpha", "2026-10-10T10:00:00Z"),
      project("prj_b", "Beta", "2026-10-20T10:00:00Z"),
    ]);
    renderPage("/providers", { routes });

    expect(await sidebar().findByRole("link", { name: "Agents" })).toHaveAttribute(
      "href",
      "/projects/prj_b/agents",
    );
    expect(sidebar().getByText("Project")).toBeInTheDocument();
    expect(sidebar().getByRole("button", { name: "Beta" })).toBeInTheDocument();
  });

  it("shows no Project group and 'Projects' when no project was ever opened", async () => {
    stubProjects([project("prj_a", "Alpha", null)]);
    renderPage("/providers", { routes });

    expect(await sidebar().findByRole("button", { name: "Projects" })).toBeInTheDocument();
    expect(sidebar().queryByRole("link", { name: "Agents" })).not.toBeInTheDocument();
    expect(sidebar().queryByText("Project")).not.toBeInTheDocument();
  });

  it("keeps the project of the URL when going to an app page, whatever last_opened_at says", async () => {
    const user = userEvent.setup();
    stubProjects([
      project("prj_a", "Alpha", "2026-10-10T10:00:00Z"),
      project("prj_b", "Beta", "2026-10-20T10:00:00Z"),
    ]);
    renderPage("/projects/prj_a/agents", { routes });

    expect(await sidebar().findByRole("button", { name: "Alpha" })).toBeInTheDocument();
    await user.click(sidebar().getByRole("link", { name: "AI providers" }));

    expect(await screen.findByText("Providers Page")).toBeInTheDocument();
    expect(sidebar().getByRole("button", { name: "Alpha" })).toBeInTheDocument();
    expect(sidebar().getByRole("link", { name: "Agents" })).toHaveAttribute(
      "href",
      "/projects/prj_a/agents",
    );
  });

  it("puts a full-width switcher below the app name, as tall as the nav items", async () => {
    const longName = "A very long project name that is shown in full";
    stubProjects([project("prj_a", longName, "2026-10-10T10:00:00Z")]);
    renderPage("/providers", { routes });

    const trigger = await sidebar().findByRole("button", { name: longName });
    expect(trigger.className).not.toContain("max-w-[130px]");
    // The default Button size is as tall as the nav items; a size override would break that.
    expect(trigger).toHaveAttribute("data-size", "default");
    expect(trigger).toHaveClass("w-full");
    const heading = sidebar().getByRole("heading", { level: 1 });
    expect(heading.parentElement).toHaveClass("flex-col");
    expect(heading.parentElement).toBe(trigger.parentElement);
    expect(
      heading.compareDocumentPosition(trigger) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("makes the dropdown as wide as the trigger", async () => {
    const user = userEvent.setup();
    stubProjects([project("prj_a", "Alpha", "2026-10-10T10:00:00Z")]);
    renderPage("/providers", { routes });

    await user.click(await sidebar().findByRole("button", { name: "Alpha" }));

    expect(await screen.findByRole("menu")).toHaveClass("w-(--radix-dropdown-menu-trigger-width)");
  });
});
