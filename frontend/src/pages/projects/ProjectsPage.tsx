import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { ProjectStatus } from "@/components/ProjectStatus";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, errorText, unwrap } from "@/lib/api";

function formatLastOpened(lastOpenedAt: string | null): string {
  if (!lastOpenedAt) return "Never";
  return new Date(lastOpenedAt).toLocaleString();
}

export function ProjectsPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const {
    data: projects,
    error,
    isPending,
    isError,
  } = useQuery({
    queryKey: ["projects"],
    queryFn: async () => {
      const res = await api.GET("/api/projects");
      return unwrap(res);
    },
  });

  const openMutation = useMutation({
    mutationFn: async (projectId: string) => {
      const res = await api.POST("/api/projects/{project_id}/open", {
        params: { path: { project_id: projectId } },
      });
      return unwrap(res);
    },
    onSuccess: (_data, projectId) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      void navigate(`/projects/${projectId}`);
    },
    onError: (err) => {
      setActionError(errorText(err));
      setOpeningId(null);
    },
  });

  const sortedProjects = useMemo(() => {
    if (!projects) return [];
    return [...projects].sort((a, b) => {
      if (a.last_opened_at && b.last_opened_at) {
        return new Date(b.last_opened_at).getTime() - new Date(a.last_opened_at).getTime();
      }
      if (a.last_opened_at) return -1;
      if (b.last_opened_at) return 1;
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });
  }, [projects]);

  const handleOpenProject = (id: string) => {
    setActionError(null);
    setOpeningId(id);
    openMutation.mutate(id);
  };

  return (
    <div className="container mx-auto max-w-5xl space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Projects</h1>
          <p className="text-sm text-muted-foreground">
            Manage your local and GitHub repositories.
          </p>
        </div>
        <Button asChild>
          <Link to="/projects/new">
            <Plus className="mr-1.5 size-4" />
            Add project
          </Link>
        </Button>
      </div>

      {actionError && (
        <div role="alert" className="rounded-md bg-destructive/15 p-3 text-sm text-destructive">
          {actionError}
        </div>
      )}

      {isPending && (
        <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
          <Loader2 className="mr-2 size-4 animate-spin" />
          Loading projects...
        </div>
      )}

      {isError && (
        <div role="alert" className="rounded-md bg-destructive/15 p-4 text-sm text-destructive">
          {errorText(error)}
        </div>
      )}

      {!isPending && !isError && sortedProjects.length === 0 && (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed p-12 text-center">
          <p className="mb-4 text-muted-foreground">No projects yet.</p>
          <Button asChild variant="outline">
            <Link to="/projects/new">
              <Plus className="mr-1.5 size-4" />
              Add project
            </Link>
          </Button>
        </div>
      )}

      {!isPending && !isError && sortedProjects.length > 0 && (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Source</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Last opened</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sortedProjects.map((project) => {
                const isOpening = openingId === project.id;
                return (
                  <TableRow key={project.id}>
                    <TableCell className="font-medium">
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => {
                            handleOpenProject(project.id);
                          }}
                          disabled={openingId !== null}
                          className="cursor-pointer text-left font-medium text-primary hover:underline disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          {project.name}
                        </button>
                        {isOpening && (
                          <span className="flex items-center gap-1 text-xs text-muted-foreground">
                            <Loader2 className="size-3 animate-spin" />
                            Opening...
                          </span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell>{project.source === "local" ? "Local folder" : "GitHub"}</TableCell>
                    <TableCell>
                      <ProjectStatus
                        status={project.status}
                        status_detail={project.status_detail}
                      />
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatLastOpened(project.last_opened_at)}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
