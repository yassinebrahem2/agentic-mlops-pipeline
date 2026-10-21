import { RouterProvider } from "react-router";
import { buildRouter } from "./app/router";
import { pageRoutes } from "./app/routes";

const router = buildRouter(pageRoutes);

export function App() {
  return <RouterProvider router={router} />;
}
