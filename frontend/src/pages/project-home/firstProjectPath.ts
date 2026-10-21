import type { PageRoute } from "@/app/routes";

export function firstProjectPath(routes: PageRoute[], projectId: string): string | null {
  const projectRoutes = routes
    .filter((r) => r.nav?.area === "project")
    .sort((a, b) => {
      const orderA = a.nav?.order ?? 0;
      const orderB = b.nav?.order ?? 0;
      if (orderA !== orderB) {
        return orderA - orderB;
      }
      return (a.nav?.label ?? "").localeCompare(b.nav?.label ?? "");
    });

  const firstRoute = projectRoutes[0];
  if (!firstRoute) {
    return null;
  }

  return firstRoute.path.replaceAll(":projectId", projectId);
}
