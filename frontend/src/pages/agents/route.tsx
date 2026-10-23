import type { PageRoute } from "@/app/routes";
import { AgentsPage } from "./AgentsPage";

export const route: PageRoute = {
  path: "/projects/:projectId/agents",
  element: <AgentsPage />,
  nav: { area: "project", label: "Agents", order: 0 },
};
