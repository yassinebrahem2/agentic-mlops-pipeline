import { screen } from "@testing-library/react";
import { useParams } from "react-router";
import { describe, expect, it, vi } from "vitest";
import type { PageRoute } from "@/app/routes";
import { renderPage } from "@/test/render";
import { route as projectHomeRoute } from "./route";

const mockRoutes = vi.hoisted(() => ({ pageRoutes: [] as PageRoute[] }));

vi.mock("@/app/routes", () => {
  return {
    get pageRoutes() {
      return mockRoutes.pageRoutes;
    },
  };
});

function SettingsPage() {
  const { projectId } = useParams();
  return <div>Project Settings for {projectId}</div>;
}

const fakeSettingsRoute: PageRoute = {
  path: "/projects/:projectId/settings",
  element: <SettingsPage />,
  nav: { area: "project", label: "Project settings", order: 100 },
};

describe("ProjectHome", () => {
  it("redirects to the first project-area route with projectId substituted", async () => {
    mockRoutes.pageRoutes = [fakeSettingsRoute];

    renderPage("/projects/prj_123", { routes: [projectHomeRoute, fakeSettingsRoute] });

    expect(await screen.findByText("Project Settings for prj_123")).toBeInTheDocument();
  });

  it("shows fallback message if no project-area route exists", async () => {
    mockRoutes.pageRoutes = [];

    renderPage("/projects/prj_123", { routes: [projectHomeRoute] });

    expect(await screen.findByText("No project pages yet.")).toBeInTheDocument();
  });
});
