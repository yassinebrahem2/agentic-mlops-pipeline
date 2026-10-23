import { createBrowserRouter, type DataRouter, type RouteObject } from "react-router";
import { AuthGate } from "./auth/AuthGate";
import { NotFound } from "./NotFound";
import type { PageRoute } from "./routes";
import { AppShell } from "./shell/AppShell";

export function buildRouter(
  routes: PageRoute[],
  createRouter: (routes: RouteObject[]) => DataRouter = createBrowserRouter,
  options?: { skipAuthGate?: boolean },
): DataRouter {
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

  const shell = <AppShell routes={routes} />;

  const routeTree: RouteObject[] = [
    {
      element: options?.skipAuthGate ? shell : <AuthGate>{shell}</AuthGate>,
      children,
    },
  ];

  return createRouter(routeTree);
}
