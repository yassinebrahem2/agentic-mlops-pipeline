import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "react-router";
import { buildRouter } from "./app/router";
import { pageRoutes } from "./app/routes";
import { queryClient } from "./lib/query";

const router = buildRouter(pageRoutes);

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
