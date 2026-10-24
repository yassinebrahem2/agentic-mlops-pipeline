import { useState } from "react";
import { Menu } from "lucide-react";
import { Link, Outlet } from "react-router";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { APP_NAME } from "@/config";
import type { PageRoute } from "../routes";
import { GearMenu } from "./GearMenu";
import { Sidebar } from "./Sidebar";

export function AppShell({ routes }: { routes: PageRoute[] }) {
  const [open, setOpen] = useState(false);
  const appRoutes = routes.filter((r) => r.nav?.area === "app");

  return (
    <div className="flex h-dvh w-full flex-col md:flex-row">
      <header className="flex h-14 shrink-0 items-center justify-between border-b px-4 md:hidden">
        <Button
          variant="ghost"
          size="icon"
          aria-label="Open sidebar"
          onClick={() => {
            setOpen(true);
          }}
        >
          <Menu className="size-5" />
          <span className="sr-only">Open sidebar</span>
        </Button>
        <Link to="/" className="text-lg font-semibold tracking-tight">
          {APP_NAME}
        </Link>
        <GearMenu appRoutes={appRoutes} />
      </header>

      {/* The repository has no shadcn Sheet, so the narrow-screen sidebar is a Dialog pinned to the left edge. */}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          className="fixed inset-y-0 left-0 top-0 z-50 h-full w-64 max-w-none translate-x-0 translate-y-0 rounded-none border-r p-0 shadow-lg"
          showCloseButton={true}
        >
          <DialogTitle className="sr-only">Navigation</DialogTitle>
          <DialogDescription className="sr-only">Main navigation menu</DialogDescription>
          <Sidebar
            routes={routes}
            onNavigate={() => {
              setOpen(false);
            }}
            className="w-full border-r-0"
          />
        </DialogContent>
      </Dialog>

      <div className="hidden h-full shrink-0 md:flex">
        <Sidebar routes={routes} />
      </div>

      <main className="min-h-0 flex-1 overflow-auto">
        <Outlet context={routes} />
      </main>
    </div>
  );
}
