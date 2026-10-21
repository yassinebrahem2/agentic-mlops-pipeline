import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { createMemoryRouter, RouterProvider } from "react-router";
import { buildRouter } from "../router";
import type { PageRoute } from "../routes";

const fakeAppRoute: PageRoute = {
  path: "/settings",
  element: <div>App Settings Page</div>,
  nav: { area: "app", label: "Settings", order: 1 },
};

const fakeProjectRoute: PageRoute = {
  path: "/projects/:projectId/agents",
  element: <div>Project Agents Page</div>,
  nav: { area: "project", label: "Agents", order: 1 },
};

const fakeProjectsNewRoute: PageRoute = {
  path: "/projects/new",
  element: <div>New Project Page</div>,
};

const testRoutes: PageRoute[] = [fakeAppRoute, fakeProjectRoute, fakeProjectsNewRoute];

function renderWithRouter(routes: PageRoute[], initialEntries: string[]) {
  const router = buildRouter(routes, (tree) => createMemoryRouter(tree, { initialEntries }));
  return render(<RouterProvider router={router} />);
}

describe("AppShell and Router", () => {
  it("renders the app entry in the sidebar at / and shows 'No pages yet.'", () => {
    renderWithRouter(testRoutes, ["/"]);

    const appLink = screen.getByRole("link", { name: "Settings" });
    expect(appLink).toBeInTheDocument();
    expect(appLink).toHaveAttribute("href", "/settings");

    expect(screen.queryByRole("link", { name: "Agents" })).not.toBeInTheDocument();

    expect(screen.getByText("No pages yet.")).toBeInTheDocument();
  });

  it("shows the project entry with projectId substituted under /projects/prj_1/agents", () => {
    renderWithRouter(testRoutes, ["/projects/prj_1/agents"]);

    expect(screen.getByText("Project Agents Page")).toBeInTheDocument();

    const projectLink = screen.getByRole("link", { name: "Agents" });
    expect(projectLink).toBeInTheDocument();
    expect(projectLink).toHaveAttribute("href", "/projects/prj_1/agents");

    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
  });

  it("does not show project entries at /projects/new", () => {
    renderWithRouter(testRoutes, ["/projects/new"]);

    expect(screen.getByText("New Project Page")).toBeInTheDocument();

    expect(screen.queryByRole("link", { name: "Agents" })).not.toBeInTheDocument();

    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
  });

  it("shows NotFound on an unknown path", () => {
    renderWithRouter(testRoutes, ["/unknown-page"]);

    expect(screen.getByText("This page does not exist.")).toBeInTheDocument();
    const homeLink = screen.getByRole("link", { name: /home/i });
    expect(homeLink).toBeInTheDocument();
    expect(homeLink).toHaveAttribute("href", "/");
  });

  it("shows navigation entries when opening the narrow-screen sidebar via button", async () => {
    const user = userEvent.setup();
    renderWithRouter(testRoutes, ["/"]);

    const openButton = screen.getByRole("button", { name: /open sidebar/i });
    expect(openButton).toBeInTheDocument();

    await user.click(openButton);

    const dialog = screen.getByRole("dialog");
    expect(dialog).toBeInTheDocument();
    const settingsLinks = screen.getAllByRole("link", { name: "Settings" });
    expect(settingsLinks.length).toBeGreaterThanOrEqual(1);
  });

  it("shows registered page at / instead of 'No pages yet.' when a module registers /", () => {
    const routesWithRoot: PageRoute[] = [
      ...testRoutes,
      {
        path: "/",
        element: <div>Registered Home Page</div>,
      },
    ];

    renderWithRouter(routesWithRoot, ["/"]);

    expect(screen.getByText("Registered Home Page")).toBeInTheDocument();
    expect(screen.queryByText("No pages yet.")).not.toBeInTheDocument();
  });
});
