import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router";
import { buildRouter } from "./app/router";
import { pageRoutes } from "@/app/routes";
import { queryClient } from "./lib/query";

let router: ReturnType<typeof buildRouter> | null = null;

function getRouter() {
  if (!router) {
    router = buildRouter(pageRoutes);
  }
  return router;
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={getRouter()} />
    </QueryClientProvider>
  );
}
