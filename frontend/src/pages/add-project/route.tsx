import type { PageRoute } from "@/app/routes";
import { AddProjectPage } from "./AddProjectPage";

export const route: PageRoute = {
  path: "/projects/new",
  element: <AddProjectPage />,
};
