import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Label } from "@/components/ui/label";
import { ApiError, api, errorText, unwrap } from "@/lib/api";
import { projectsKey } from "@/lib/useProjects";
import { onboardingPath } from "@/pages/onboarding/paths";

export function GitHubTab() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [repoInput, setRepoInput] = useState("");
  const [addingRepo, setAddingRepo] = useState<string | null>(null);
  const [githubError, setGithubError] = useState<{
    message: string;
    projectId?: string;
  } | null>(null);

  const {
    data: githubStatus,
    isPending: isStatusPending,
    error: statusError,
  } = useQuery({
    queryKey: ["github"],
    queryFn: async () => {
      const res = await api.GET("/api/github");
      return unwrap(res);
    },
  });

  const isConnected = githubStatus && (githubStatus.mode === "gh" || githubStatus.mode === "token");

  const {
    data: repos,
    isPending: isReposPending,
    error: reposError,
  } = useQuery({
    queryKey: ["github", "repos"],
    queryFn: async () => {
      const res = await api.GET("/api/github/repos");
      return unwrap(res);
    },
    enabled: Boolean(isConnected),
  });

  const addProjectMutation = useMutation({
    mutationFn: async (repo: string) => {
      const res = await api.POST("/api/projects", {
        body: {
          source: "github",
          repo,
        },
      });
      return unwrap(res);
    },
    onSuccess: (project) => {
      setAddingRepo(null);
      void queryClient.invalidateQueries({ queryKey: projectsKey });
      void navigate(onboardingPath(project.id));
    },
    onError: (err) => {
      setAddingRepo(null);
      if (err instanceof ApiError && err.code === "project_exists") {
        const detail = err.detail as { project_id?: string } | undefined;
        setGithubError({
          message: err.message,
          projectId: detail?.project_id,
        });
        return;
      }
      setGithubError({
        message: errorText(err),
      });
    },
  });

  const repoOptions = useMemo(() => {
    if (!repos) return [];
    return repos.map((repo) => ({
      value: repo.full_name,
      label: repo.full_name,
      detail: repo.description ?? undefined,
    }));
  }, [repos]);

  const handleAdd = () => {
    const trimmed = repoInput.trim();
    if (!trimmed) return;
    setGithubError(null);
    setAddingRepo(trimmed);
    addProjectMutation.mutate(trimmed);
  };

  if (isStatusPending) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
        <Loader2 className="mr-2 size-4 animate-spin" />
        Checking GitHub connection...
      </div>
    );
  }

  if (statusError) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {errorText(statusError)}
      </p>
    );
  }

  if (!isConnected) {
    return (
      <div className="rounded-lg border p-6">
        <p className="text-sm text-muted-foreground">
          Connect GitHub in{" "}
          <Link to="/integrations" className="text-primary underline">
            Integrations
          </Link>{" "}
          first.
        </p>
      </div>
    );
  }

  const isAdding = Boolean(addingRepo);

  return (
    <div className="space-y-6">
      {githubError && (
        <div
          role="alert"
          className="flex flex-col gap-2 rounded-md bg-destructive/15 p-4 text-sm text-destructive sm:flex-row sm:items-center sm:justify-between"
        >
          <span>{githubError.message}</span>
          {githubError.projectId && (
            <Button asChild variant="outline" size="sm">
              <Link to={`/projects/${githubError.projectId}`}>Open it</Link>
            </Button>
          )}
        </div>
      )}

      {reposError && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(reposError)}
        </p>
      )}

      <div className="space-y-2 rounded-lg border p-4">
        <Label htmlFor="github-repo-input" className="text-sm font-medium">
          Repository
        </Label>
        <div className="flex gap-2">
          <Combobox
            id="github-repo-input"
            label="Repository"
            value={repoInput}
            onValueChange={setRepoInput}
            options={repoOptions}
            loading={isReposPending}
            placeholder="owner/name or URL"
            emptyText="No repositories found"
            disabled={isAdding}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                handleAdd();
              }
            }}
          />
          <Button type="button" onClick={handleAdd} disabled={isAdding || !repoInput.trim()}>
            {isAdding ? <Loader2 className="mr-1.5 size-4 animate-spin" /> : null}
            Add
          </Button>
        </div>

        {isAdding && (
          <div className="flex items-center gap-2 pt-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin text-primary" />
            <span>Downloading {addingRepo}...</span>
          </div>
        )}
      </div>
    </div>
  );
}
