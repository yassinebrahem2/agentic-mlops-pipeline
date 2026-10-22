import type { PageRoute } from "@/app/routes";
import { ProjectHome } from "./ProjectHome";

export const route: PageRoute = {
  path: "/projects/:projectId",
  element: <ProjectHome />,
};
