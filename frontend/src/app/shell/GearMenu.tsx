import { LogOut, Settings } from "lucide-react";
import { Link } from "react-router";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAuth } from "../auth/useAuth";
import type { PageRoute } from "../routes";

export function GearMenu({ appRoutes }: { appRoutes: PageRoute[] }) {
  const navRoutes = appRoutes.filter((r) => r.nav !== undefined);
  const auth = useAuth();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Settings">
          <Settings className="size-4" />
          <span className="sr-only">Settings</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-48">
        <DropdownMenuLabel>Settings</DropdownMenuLabel>
        {navRoutes.length > 0 && <DropdownMenuSeparator />}
        {navRoutes.map((route) => {
          const Icon = route.nav?.icon;
          return (
            <DropdownMenuItem key={route.path} asChild>
              <Link to={route.path} className="flex items-center gap-2">
                {Icon && <Icon className="size-4 shrink-0" />}
                <span>{route.nav?.label}</span>
              </Link>
            </DropdownMenuItem>
          );
        })}
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={() => {
            void auth.signOut();
          }}
          className="flex items-center gap-2 cursor-pointer"
        >
          <LogOut className="size-4 shrink-0" />
          <span>Sign out</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
