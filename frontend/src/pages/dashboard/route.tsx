import { LayoutDashboard } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { DashboardPage } from "./DashboardPage";

export const route: PageRoute = {
  path: "/projects/:projectId/dashboard",
  element: <DashboardPage />,
  // The lowest order of the project pages, so the project's landing page is the Dashboard.
  nav: { area: "project", label: "Dashboard", order: -10, icon: LayoutDashboard },
};
