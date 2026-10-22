import { describe, expect, it } from "vitest";
import type { PageRoute } from "@/app/routes";
import { firstProjectPath } from "./firstProjectPath";

describe("firstProjectPath", () => {
  it("picks the lower order among project routes and fills in the projectId", () => {
    const routes: PageRoute[] = [
      {
        path: "/projects/:projectId/settings",
        element: null,
        nav: { area: "project", label: "Settings", order: 100 },
      },
      {
        path: "/projects/:projectId/agents",
        element: null,
        nav: { area: "project", label: "Agents", order: 0 },
      },
      {
        path: "/settings",
        element: null,
        nav: { area: "app", label: "App Settings", order: 10 },
      },
    ];

    const result = firstProjectPath(routes, "prj_test123");
    expect(result).toBe("/projects/prj_test123/agents");
  });

  it("returns null when there are no project-area routes", () => {
    const routes: PageRoute[] = [
      {
        path: "/",
        element: null,
        nav: { area: "app", label: "Projects", order: 0 },
      },
      {
        path: "/projects/new",
        element: null,
      },
    ];

    const result = firstProjectPath(routes, "prj_test123");
    expect(result).toBeNull();
  });

  it("returns null on an empty route list", () => {
    expect(firstProjectPath([], "prj_test123")).toBeNull();
  });
});
