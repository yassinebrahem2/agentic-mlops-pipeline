import * as React from "react";
import { QueryClientContext, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronsUpDown, FolderKanban } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { api, unwrap } from "@/lib/api";
import { cn } from "@/lib/utils";

function ConnectedProjectSwitcher({
  onNavigate,
  className,
}: {
  onNavigate?: () => void;
  className?: string;
}) {
  const { projectId } = useParams<{ projectId?: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const { data: projects } = useQuery({
    queryKey: ["projects"],
    queryFn: async () => {
      const res = await api.GET("/api/projects");
      return unwrap(res);
    },
  });

  const openMutation = useMutation({
    mutationFn: async (targetId: string) => {
      const res = await api.POST("/api/projects/{project_id}/open", {
        params: { path: { project_id: targetId } },
      });
      return unwrap(res);
    },
    onSuccess: (_data, targetId) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      onNavigate?.();
      void navigate(`/projects/${targetId}`);
    },
  });

  const currentProject =
    projectId && projects ? projects.find((p) => p.id === projectId) : undefined;
  const label = currentProject ? currentProject.name : "Projects";

  return (
    <DropdownMenu>
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
        {projects && projects.length > 0 && (
          <div className="max-h-60 overflow-y-auto">
            {projects.map((p) => {
              const isSelected = p.id === projectId;
              return (
                <DropdownMenuItem
                  key={p.id}
                  onClick={() => {
                    openMutation.mutate(p.id);
                  }}
                  className="flex items-center justify-between"
                >
                  <span className="truncate">{p.name}</span>
                  {isSelected && <Check className="ml-2 size-3.5 shrink-0" />}
                </DropdownMenuItem>
              );
            })}
          </div>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/" onClick={onNavigate} className="flex items-center gap-2">
            <FolderKanban className="size-3.5" />
            <span>All projects</span>
          </Link>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function ProjectSwitcher(props: { onNavigate?: () => void; className?: string }) {
  const hasQueryClient = Boolean(React.useContext(QueryClientContext));
  if (!hasQueryClient) {
    return (
      <Button
        variant="outline"
        size="sm"
        aria-label="Projects"
        className={cn("h-8 max-w-[130px] justify-between text-xs font-normal", props.className)}
      >
        <span className="truncate">Projects</span>
        <ChevronsUpDown className="ml-1 size-3 shrink-0 opacity-50" />
      </Button>
    );
  }

  return <ConnectedProjectSwitcher {...props} />;
}
