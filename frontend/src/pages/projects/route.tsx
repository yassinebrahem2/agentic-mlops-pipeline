import { FolderKanban } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { PROJECTS_PATH } from "./paths";
import { ProjectsPage } from "./ProjectsPage";

export const route: PageRoute = {
  path: PROJECTS_PATH,
  element: <ProjectsPage />,
  nav: {
    area: "app",
    label: "Projects",
    order: 0,
    icon: FolderKanban,
  },
};
