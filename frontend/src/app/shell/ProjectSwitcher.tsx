import * as React from "react";
import { Check, ChevronsUpDown, FolderKanban, Loader2 } from "lucide-react";
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
import { errorText } from "@/lib/api";
import { useCurrentProject } from "@/lib/useCurrentProject";
import { useOpenProject } from "@/lib/useOpenProject";
import { useProjects } from "@/lib/useProjects";
import { cn } from "@/lib/utils";

export function ProjectSwitcher({
  onNavigate,
  className,
}: {
  onNavigate?: () => void;
  className?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const { projectId, project: currentProject } = useCurrentProject();
  const {
    openProject,
    openingId,
    error: openError,
  } = useOpenProject({
    onNavigate: () => {
      setOpen(false);
      onNavigate?.();
    },
  });

  React.useEffect(() => {
    if (openError) {
      setOpen(true);
    }
  }, [openError]);

  const { data: projects, error: projectsError } = useProjects();

  const label = currentProject ? currentProject.name : "Projects";

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          aria-label={label}
          className={cn("h-8 max-w-[130px] justify-between text-xs font-normal", className)}
        >
          <span className="truncate">{label}</span>
          <ChevronsUpDown className="ml-1 size-3 shrink-0 opacity-50" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56">
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
          Projects
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {projectsError && (
          <div role="alert" className="px-2 py-1.5 text-xs text-destructive">
            {errorText(projectsError)}
          </div>
        )}
        {openError && (
          <>
            <div role="alert" className="px-2 py-1.5 text-xs text-destructive">
              {openError}
            </div>
            <DropdownMenuSeparator />
          </>
        )}
        {projects && projects.length > 0 && (
          <div className="max-h-60 overflow-y-auto">
            {projects.map((p) => {
              const isSelected = p.id === projectId;
              const isOpening = openingId === p.id;
              return (
                <DropdownMenuItem
                  key={p.id}
                  onClick={() => {
                    setOpen(false);
                    openProject(p.id);
                  }}
                  disabled={openingId !== null}
                  className="flex items-center justify-between"
                >
                  <span className="truncate">{p.name}</span>
                  {isOpening && (
                    <Loader2 className="ml-2 size-3 animate-spin text-muted-foreground" />
                  )}
                  {!isOpening && isSelected && <Check className="ml-2 size-3.5 shrink-0" />}
                </DropdownMenuItem>
              );
            })}
          </div>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link
            to="/"
            onClick={() => {
              setOpen(false);
              onNavigate?.();
            }}
            className="flex items-center gap-2"
          >
            <FolderKanban className="size-3.5" />
            <span>All projects</span>
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
