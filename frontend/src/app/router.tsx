import { createBrowserRouter, type RouteObject } from "react-router";
import { NotFound } from "./NotFound";
import type { PageRoute } from "./routes";
import { AppShell } from "./shell/AppShell";

export function buildRouter<T = ReturnType<typeof createBrowserRouter>>(
  routes: PageRoute[],
  createRouter: (routes: RouteObject[]) => T = createBrowserRouter as unknown as (
    routes: RouteObject[],
  ) => T,
): T {
  const hasRootRoute = routes.some((r) => r.path === "/");

  const children: RouteObject[] = [
    ...(hasRootRoute
      ? []
      : [
          {
            path: "/",
            element: (
              <div className="flex h-full items-center justify-center p-8 text-center text-muted-foreground">
                No pages yet.
              </div>
            ),
          },
        ]),
    ...routes.map((r) => ({
      path: r.path,
      element: r.element,
    })),
    {
      path: "*",
      element: <NotFound />,
    },
  ];

  const routeTree: RouteObject[] = [
    {
      element: <AppShell routes={routes} />,
      children,
    },
  ];

  return createRouter(routeTree);
}
