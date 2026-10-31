import { ScrollText } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { ActivityPage } from "./ActivityPage";

export const route: PageRoute = {
  path: "/projects/:projectId/activity",
  element: <ActivityPage />,
  nav: { area: "project", label: "Activity", order: 50, icon: ScrollText },
};
