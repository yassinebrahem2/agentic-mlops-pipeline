import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { buildRouter } from "../app/router";
import { pageRoutes, type PageRoute } from "../app/routes";

export function renderPage(path: string, options?: { routes?: PageRoute[] }): RenderResult {
  const router = buildRouter(options?.routes ?? pageRoutes, (routes) =>
    createMemoryRouter(routes, { initialEntries: [path], initialIndex: 0 }),
  );

  const testQueryClient = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

  return render(
    <QueryClientProvider client={testQueryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}
