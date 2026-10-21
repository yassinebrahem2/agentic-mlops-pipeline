import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PageRoute } from "@/app/routes";
import { renderPage } from "@/test/render";
import * as firstPathModule from "./firstProjectPath";
import { route as projectHomeRoute } from "./route";

const fakeSettingsRoute: PageRoute = {
  path: "/projects/:projectId/settings",
  element: <div>Project Settings Page Content</div>,
  nav: { area: "project", label: "Project settings", order: 100 },
};

describe("ProjectHome", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("redirects to the first project-area route with projectId substituted", async () => {
    vi.spyOn(firstPathModule, "firstProjectPath").mockReturnValue("/projects/prj_123/settings");

    renderPage("/projects/prj_123", { routes: [projectHomeRoute, fakeSettingsRoute] });

    expect(await screen.findByText("Project Settings Page Content")).toBeInTheDocument();
  });

  it("shows fallback message if no project-area route exists", async () => {
    vi.spyOn(firstPathModule, "firstProjectPath").mockReturnValue(null);

    renderPage("/projects/prj_123", { routes: [projectHomeRoute] });

    expect(await screen.findByText("No project pages yet.")).toBeInTheDocument();
  });
});
