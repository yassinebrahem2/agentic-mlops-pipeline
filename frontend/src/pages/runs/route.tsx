import { Activity } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { RunsPage } from "./RunsPage";

export const route: PageRoute = {
  path: "/projects/:projectId/runs",
  element: <RunsPage />,
  nav: {
    area: "project",
    label: "Runs",
    order: 40,
    icon: Activity,
  },
};
