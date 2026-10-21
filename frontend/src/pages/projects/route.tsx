import { FolderKanban } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { ProjectsPage } from "./ProjectsPage";

export const route: PageRoute = {
  path: "/",
  element: <ProjectsPage />,
  nav: {
    area: "app",
    label: "Projects",
    order: 0,
    icon: FolderKanban,
  },
};
