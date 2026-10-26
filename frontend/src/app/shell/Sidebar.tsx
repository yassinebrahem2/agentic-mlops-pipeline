import { Link, NavLink } from "react-router";
import { Badge } from "@/components/ui/badge";
import { APP_NAME } from "@/config";
import { useCurrentProject } from "@/lib/useCurrentProject";
import { cn } from "@/lib/utils";
import type { PageRoute } from "../routes";
import { GearMenu } from "./GearMenu";
import { ProjectSwitcher } from "./ProjectSwitcher";

function NavItem({
  route,
  to,
  projectId,
  onNavigate,
}: {
  route: PageRoute;
  to: string;
  projectId?: string;
  onNavigate?: () => void;
}) {
  const Icon = route.nav?.icon;
  const count = route.nav?.badge ? route.nav.badge(projectId) : undefined;
  const showBadge = typeof count === "number" && count > 0;

  return (
    <NavLink
      to={to}
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors",
          isActive
            ? "bg-secondary text-secondary-foreground"
            : "text-muted-foreground hover:bg-muted hover:text-foreground",
        )
      }
    >
      {Icon && <Icon className="size-4 shrink-0" />}
      <span className="flex-1 truncate">{route.nav?.label}</span>
      {showBadge && (
        <Badge
          variant="secondary"
          className="ml-auto flex h-5 min-w-5 items-center justify-center rounded-full px-1.5 text-xs font-semibold"
        >
          {count}
        </Badge>
      )}
    </NavLink>
  );
}

export function Sidebar({
  routes,
  onNavigate,
  className,
}: {
  routes: PageRoute[];
  onNavigate?: () => void;
  className?: string;
}) {
  const { projectId } = useCurrentProject();
  const projectRoutes = routes.filter((r) => r.nav?.area === "project");
  const appRoutes = routes.filter((r) => r.nav?.area === "app");

  return (
    <aside
      className={cn(
        "flex h-full w-64 flex-col border-r bg-sidebar text-sidebar-foreground",
        className,
      )}
    >
      <div className="flex flex-col gap-3 border-b p-3">
        <h1 className="px-3 text-lg font-semibold tracking-tight">
          <Link to="/" onClick={onNavigate} className="transition-opacity hover:opacity-80">
            {APP_NAME}
          </Link>
        </h1>
        <ProjectSwitcher onNavigate={onNavigate} />
      </div>

      <nav className="flex-1 space-y-4 overflow-y-auto p-3">
        {projectId && projectRoutes.length > 0 && (
          <div className="space-y-1">
            <div className="px-3 py-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Project
            </div>
            {projectRoutes.map((route) => {
              const to = route.path.replaceAll(":projectId", projectId);
              return (
                <NavItem
                  key={route.path}
                  route={route}
                  to={to}
                  projectId={projectId}
                  onNavigate={onNavigate}
                />
              );
            })}
          </div>
        )}

        {appRoutes.length > 0 && (
          <div className="space-y-1">
            {projectId && (
              <div className="px-3 py-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                App
              </div>
            )}
            {appRoutes.map((route) => (
              <NavItem
                key={route.path}
                route={route}
                to={route.path}
                projectId={projectId}
                onNavigate={onNavigate}
              />
            ))}
          </div>
        )}
      </nav>

      <div className="border-t p-3">
        <GearMenu appRoutes={appRoutes} />
      </div>
    </aside>
  );
}
