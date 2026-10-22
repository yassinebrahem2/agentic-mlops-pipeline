import { Settings } from "lucide-react";
import type { PageRoute } from "../../app/routes";
import { AppSettingsPage } from "./AppSettingsPage";

export const route: PageRoute = {
  path: "/settings",
  element: <AppSettingsPage />,
  nav: { area: "app", label: "App settings", order: 30, icon: Settings },
};
