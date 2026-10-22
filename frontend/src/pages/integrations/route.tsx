import { Plug } from "lucide-react";
import type { PageRoute } from "../../app/routes";
import { IntegrationsPage } from "./IntegrationsPage";

export const route: PageRoute = {
  path: "/integrations",
  element: <IntegrationsPage />,
  nav: { area: "app", label: "Integrations", order: 20, icon: Plug },
};
