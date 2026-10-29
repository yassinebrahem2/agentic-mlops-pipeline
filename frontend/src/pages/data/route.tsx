import { Database } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { DataPage } from "./DataPage";

export const route: PageRoute = {
  path: "/projects/:projectId/data",
  element: <DataPage />,
  nav: {
    area: "project",
    label: "Data",
    order: 30,
    icon: Database,
  },
};
