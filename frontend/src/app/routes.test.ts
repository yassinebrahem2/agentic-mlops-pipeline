import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { collectRoutes, type PageRoute } from "./routes";

describe("collectRoutes", () => {
  it("returns every module's route", () => {
    const modules: Record<string, { route: PageRoute }> = {
      "../pages/projects/route.tsx": {
        route: {
          path: "/projects",
          element: createElement("div", null, "Projects"),
          nav: { area: "app", label: "Projects", order: 10 },
        },
      },
      "../pages/providers/route.tsx": {
        route: {
          path: "/providers",
          element: createElement("div", null, "Providers"),
          nav: { area: "app", label: "Providers", order: 20 },
        },
      },
    };

    const routes = collectRoutes(modules);
    expect(routes).toHaveLength(2);
    expect(routes.map((r) => r.path)).toEqual(["/projects", "/providers"]);
  });

  it("rejects a duplicate path naming both modules", () => {
    const modules: Record<string, { route: PageRoute }> = {
      "../pages/first/route.tsx": {
        route: {
          path: "/duplicate",
          element: createElement("div", null, "First"),
        },
      },
      "../pages/second/route.tsx": {
        route: {
          path: "/duplicate",
          element: createElement("div", null, "Second"),
        },
      },
    };

    expect(() => collectRoutes(modules)).toThrow(
      /duplicate.*\/duplicate.*first\/route\.tsx.*second\/route\.tsx/i,
    );
  });

  it("orders nav entries by area, then order, then label", () => {
    const modules: Record<string, { route: PageRoute }> = {
      "../pages/proj-b/route.tsx": {
        route: {
          path: "/projects/:projectId/b",
          element: createElement("div", null, "B"),
          nav: { area: "project", label: "B Label", order: 20 },
        },
      },
      "../pages/app-c/route.tsx": {
        route: {
          path: "/app/c",
          element: createElement("div", null, "C"),
          nav: { area: "app", label: "C Label", order: 20 },
        },
      },
      "../pages/app-b/route.tsx": {
        route: {
          path: "/app/b",
          element: createElement("div", null, "B"),
          nav: { area: "app", label: "B Label", order: 10 },
        },
      },
      "../pages/app-a/route.tsx": {
        route: {
          path: "/app/a",
          element: createElement("div", null, "A"),
          nav: { area: "app", label: "A Label", order: 10 },
        },
      },
      "../pages/proj-a/route.tsx": {
        route: {
          path: "/projects/:projectId/a",
          element: createElement("div", null, "A"),
          nav: { area: "project", label: "A Label", order: 10 },
        },
      },
      "../pages/no-nav/route.tsx": {
        route: {
          path: "/no-nav",
          element: createElement("div", null, "No Nav"),
        },
      },
    };

    const routes = collectRoutes(modules);
    const labels = routes.map((r) => r.nav?.label ?? "NONE");

    expect(labels).toEqual(["A Label", "B Label", "C Label", "A Label", "B Label", "NONE"]);
  });
});
