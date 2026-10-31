import { BookOpen } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { KnowledgePage } from "./KnowledgePage";

export const route: PageRoute = {
  path: "/projects/:projectId/knowledge",
  element: <KnowledgePage />,
  nav: { area: "project", label: "Knowledge", order: 60, icon: BookOpen },
};
