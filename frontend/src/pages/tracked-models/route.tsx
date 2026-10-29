import { Boxes } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { TrackedModelsPage } from "./TrackedModelsPage";

export const route: PageRoute = {
  path: "/projects/:projectId/models",
  element: <TrackedModelsPage />,
  nav: {
    area: "project",
    label: "Models",
    order: 20,
    icon: Boxes,
  },
};
