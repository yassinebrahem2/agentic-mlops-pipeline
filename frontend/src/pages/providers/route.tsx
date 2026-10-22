import { Cpu } from "lucide-react";
import type { PageRoute } from "../../app/routes";
import { ProvidersPage } from "./ProvidersPage";

export const route: PageRoute = {
  path: "/providers",
  element: <ProvidersPage />,
  nav: { area: "app", label: "AI providers", order: 10, icon: Cpu },
};
