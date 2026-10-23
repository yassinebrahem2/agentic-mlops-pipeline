import type { PageRoute } from "@/app/routes";
import { ProjectSettingsPage } from "./ProjectSettingsPage";

export const route: PageRoute = {
  path: "/projects/:projectId/settings",
  element: <ProjectSettingsPage />,
  nav: { area: "project", label: "Project settings", order: 100 },
};
