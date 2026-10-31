import { BarChart3 } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { StatsPage } from "./StatsPage";

export const route: PageRoute = {
  path: "/projects/:projectId/stats",
  element: <StatsPage />,
  nav: { area: "project", label: "Stats", order: 50, icon: BarChart3 },
};
