import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { GitBranch, Loader2, Lock } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ApiError, api, errorText, unwrap } from "@/lib/api";
import { projectsKey } from "@/lib/useProjects";

export function GitHubTab() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [repoInput, setRepoInput] = useState("");
  const [filterText, setFilterText] = useState("");
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
      void navigate(`/projects/${project.id}`);
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

  const filteredRepos = useMemo(() => {
    if (!repos) return [];
    const term = filterText.trim().toLowerCase();
    if (!term) return repos;
    return repos.filter((r) => r.full_name.toLowerCase().includes(term));
  }, [repos, filterText]);

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

      <div className="space-y-2 rounded-lg border p-4">
        <Label htmlFor="github-repo-input" className="text-sm font-medium">
          Repository
        </Label>
        <div className="flex gap-2">
          <Input
            id="github-repo-input"
            aria-label="Repository"
            value={repoInput}
            onChange={(e) => {
              setRepoInput(e.target.value);
            }}
            placeholder="owner/name or URL"
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

      <div className="space-y-3 rounded-lg border p-4">
        <div className="flex items-center justify-between">
          <Label htmlFor="github-repo-filter" className="text-sm font-medium">
            Filter repositories
          </Label>
        </div>
        <Input
          id="github-repo-filter"
          aria-label="Filter repositories"
          value={filterText}
          onChange={(e) => {
            setFilterText(e.target.value);
          }}
          placeholder="Filter repositories..."
          disabled={isAdding}
        />

        <div className="min-h-48 rounded-md border bg-muted/20">
          {reposError && (
            <p role="alert" className="p-3 text-sm text-destructive">
              {errorText(reposError)}
            </p>
          )}
          {isReposPending && (
            <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
              <Loader2 className="mr-2 size-4 animate-spin" />
              Loading repositories...
            </div>
          )}

          {!isReposPending && !reposError && (
            <div className="max-h-64 overflow-y-auto p-2">
              {filteredRepos.length === 0 ? (
                <div className="py-8 text-center text-sm text-muted-foreground">
                  No repositories found
                </div>
              ) : (
                <div className="space-y-1">
                  {filteredRepos.map((repo) => (
                    <button
                      key={repo.full_name}
                      type="button"
                      disabled={isAdding}
                      onClick={() => {
                        setRepoInput(repo.full_name);
                      }}
                      className="flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm transition-colors hover:bg-muted disabled:opacity-50"
                    >
                      <div className="flex items-center gap-2 truncate">
                        {repo.private ? (
                          <Lock className="size-3.5 shrink-0 text-muted-foreground" />
                        ) : (
                          <GitBranch className="size-3.5 shrink-0 text-muted-foreground" />
                        )}
                        <span className="font-medium truncate">{repo.full_name}</span>
                      </div>
                      {repo.description && (
                        <span className="ml-2 truncate text-xs text-muted-foreground max-w-xs">
                          {repo.description}
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
