import { act, screen } from "@testing-library/react";
import { useParams } from "react-router";
import { describe, expect, it } from "vitest";
import type { PageRoute } from "@/app/routes";
import { APP_NAME } from "@/config";
import { renderPage } from "@/test/render";
import { route as projectHomeRoute } from "./route";

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
    renderPage("/projects/prj_123", { routes: [projectHomeRoute, fakeSettingsRoute] });

    expect(await screen.findByText("Project Settings for prj_123")).toBeInTheDocument();
  });

  it("renders nothing and leads to no project page while no project is known", async () => {
    const projectlessHome: PageRoute = { path: "/home", element: projectHomeRoute.element };
    renderPage("/home", { routes: [projectlessHome, fakeSettingsRoute] });

    expect(await screen.findByRole("heading", { name: APP_NAME })).toBeInTheDocument();
    await act(() => Promise.resolve());
    expect(screen.queryByText(/Project Settings for/)).not.toBeInTheDocument();
    expect(screen.queryByText("This page does not exist.")).not.toBeInTheDocument();
    expect(screen.queryByText("No project pages yet.")).not.toBeInTheDocument();
  });

  it("shows fallback message if no project-area route exists", async () => {
    renderPage("/projects/prj_123", { routes: [projectHomeRoute] });

    expect(await screen.findByText("No project pages yet.")).toBeInTheDocument();
  });
});
