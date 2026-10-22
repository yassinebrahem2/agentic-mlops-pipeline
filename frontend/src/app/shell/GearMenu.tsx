import { useState } from "react";
import { LogOut, Settings } from "lucide-react";
import { Link } from "react-router";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { errorText } from "@/lib/api";
import { useAuth } from "../auth/useAuth";
import type { PageRoute } from "../routes";

export function GearMenu({ appRoutes }: { appRoutes: PageRoute[] }) {
  const navRoutes = appRoutes.filter((r) => r.nav !== undefined);
  const auth = useAuth();
  const [error, setError] = useState<string | null>(null);

  const handleSignOut = async () => {
    setError(null);
    try {
      await auth.signOut();
    } catch (err) {
      const msg = errorText(err);
      setError(msg);
      toast.error(msg);
    }
  };

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
        {error && (
          <div role="alert" className="px-2 py-1.5 text-xs text-destructive">
            {error}
          </div>
        )}
        <DropdownMenuItem
          onSelect={(e) => {
            e.preventDefault();
            void handleSignOut();
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
