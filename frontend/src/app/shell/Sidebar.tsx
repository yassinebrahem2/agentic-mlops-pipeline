import { Link, NavLink, useParams } from "react-router";
import { APP_NAME } from "@/config";
import { cn } from "@/lib/utils";
import type { PageRoute } from "../routes";
import { GearMenu } from "./GearMenu";
import { ProjectSwitcher } from "./ProjectSwitcher";

export function Sidebar({
  routes,
  onNavigate,
  className,
}: {
  routes: PageRoute[];
  onNavigate?: () => void;
  className?: string;
}) {
  const { projectId } = useParams<{ projectId?: string }>();
  const projectRoutes = routes.filter((r) => r.nav?.area === "project");
  const appRoutes = routes.filter((r) => r.nav?.area === "app");

  return (
    <aside
      className={cn(
        "flex h-full w-64 flex-col border-r bg-sidebar text-sidebar-foreground",
        className,
      )}
    >
      <div className="flex h-14 items-center justify-between gap-2 border-b px-4">
        <h1 className="text-lg font-semibold tracking-tight shrink-0">
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
              const Icon = route.nav?.icon;
              return (
                <NavLink
                  key={route.path}
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
                  <span>{route.nav?.label}</span>
                </NavLink>
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
            {appRoutes.map((route) => {
              const Icon = route.nav?.icon;
              return (
                <NavLink
                  key={route.path}
                  to={route.path}
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
                  <span>{route.nav?.label}</span>
                </NavLink>
              );
            })}
          </div>
        )}
      </nav>

      <div className="border-t p-3">
        <GearMenu appRoutes={appRoutes} />
      </div>
    </aside>
  );
}
