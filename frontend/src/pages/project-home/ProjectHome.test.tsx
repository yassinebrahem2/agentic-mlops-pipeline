import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import type { PageRoute } from "@/app/routes";
import { renderPage } from "@/test/render";
import { setProjectHomeRoutesForTesting } from "./firstProjectPath";
import { route as projectHomeRoute } from "./route";

describe("ProjectHome", () => {
  beforeEach(() => {
    setProjectHomeRoutesForTesting(null);
  });

  afterEach(() => {
    setProjectHomeRoutesForTesting(null);
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("redirects to the first project-area route with projectId substituted", () => {
    const fakeSettingsRoute: PageRoute = {
      path: "/projects/:projectId/settings",
      element: <div>Project Settings Page Content</div>,
      nav: { area: "project", label: "Project settings", order: 100 },
    };

    const routes: PageRoute[] = [projectHomeRoute, fakeSettingsRoute];
    setProjectHomeRoutesForTesting(routes);

    renderPage("/projects/prj_123", { routes });

    expect(screen.getByText("Project Settings Page Content")).toBeInTheDocument();
  });

  it("shows fallback message if no project-area route exists", () => {
    const routes: PageRoute[] = [projectHomeRoute];
    setProjectHomeRoutesForTesting(routes);

    renderPage("/projects/prj_123", { routes });

    expect(screen.getByText("No project pages yet.")).toBeInTheDocument();
  });
});
