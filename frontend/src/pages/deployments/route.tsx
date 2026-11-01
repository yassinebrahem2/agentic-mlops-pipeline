import { Rocket } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { DeploymentsPage } from "./DeploymentsPage";

export const route: PageRoute = {
  path: "/projects/:projectId/deployments",
  element: <DeploymentsPage />,
  nav: { area: "project", label: "Deployments", order: 40, icon: Rocket },
};
