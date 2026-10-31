import { Coins } from "lucide-react";
import type { PageRoute } from "@/app/routes";
import { UsagePage } from "./UsagePage";

export const route: PageRoute = {
  path: "/usage",
  element: <UsagePage />,
  nav: { area: "app", label: "Usage", order: 25, icon: Coins },
};
