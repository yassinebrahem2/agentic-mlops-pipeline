import type { LucideIcon } from "lucide-react";
import type React from "react";

export type PageRoute = {
  path: string;
  element: React.ReactNode;
  nav?: {
    area: "app" | "project";
    label: string;
    order: number;
    icon?: LucideIcon;
  };
};

export function collectRoutes(modules: Record<string, { route: PageRoute }>): PageRoute[] {
  const seenPaths = new Map<string, string>();
  const routes: PageRoute[] = [];

  for (const [file, mod] of Object.entries(modules)) {
    const { route } = mod;
    const existingFile = seenPaths.get(route.path);
    if (existingFile !== undefined) {
      throw new Error(
        `Duplicate route path "${route.path}" found in "${existingFile}" and "${file}".`,
      );
    }
    seenPaths.set(route.path, file);
    routes.push(route);
  }

  return routes.sort((a, b) => {
    if (a.nav && b.nav) {
      if (a.nav.area !== b.nav.area) {
        return a.nav.area.localeCompare(b.nav.area);
      }
      if (a.nav.order !== b.nav.order) {
        return a.nav.order - b.nav.order;
      }
      return a.nav.label.localeCompare(b.nav.label);
    }
    if (a.nav && !b.nav) return -1;
    if (!a.nav && b.nav) return 1;
    return a.path.localeCompare(b.path);
  });
}

const globbedRoutes = import.meta.glob<{ route: PageRoute }>("../pages/**/route.tsx", {
  eager: true,
});
export const pageRoutes: PageRoute[] = collectRoutes(globbedRoutes);
