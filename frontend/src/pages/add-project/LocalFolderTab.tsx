import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { FolderBrowser } from "@/components/FolderBrowser";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ApiError, api, errorText, unwrap } from "@/lib/api";
import { projectsKey } from "@/lib/useProjects";
import { onboardingPath } from "@/pages/onboarding/paths";

interface LocalErrorState {
  code: string;
  message: string;
  repoRoot?: string;
  projectId?: string;
}

export function LocalFolderTab() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [localError, setLocalError] = useState<LocalErrorState | null>(null);
  const [initDialogOpen, setInitDialogOpen] = useState(false);
  const [pendingInitPath, setPendingInitPath] = useState<string | null>(null);
  const [initDialogMessage, setInitDialogMessage] = useState<string>("");

  const addProjectMutation = useMutation({
    mutationFn: async ({ path, initGit }: { path: string; initGit: boolean }) => {
      const res = await api.POST("/api/projects", {
        body: {
          source: "local",
          path,
          init_git: initGit,
        },
      });
      return unwrap(res);
    },
    onSuccess: (project) => {
      void queryClient.invalidateQueries({ queryKey: projectsKey });
      void navigate(onboardingPath(project.id));
    },
    onError: (err, variables) => {
      if (err instanceof ApiError) {
        if (err.code === "not_a_git_repo") {
          const detail = err.detail as { can_init?: boolean } | undefined;
          if (detail?.can_init) {
            setPendingInitPath(variables.path);
            setInitDialogMessage(err.message);
            setInitDialogOpen(true);
            setLocalError(null);
            return;
          }
        }
        if (err.code === "not_repo_root") {
          const detail = err.detail as { repo_root?: string } | undefined;
          setLocalError({
            code: err.code,
            message: err.message,
            repoRoot: detail?.repo_root,
          });
          return;
        }
        if (err.code === "project_exists") {
          const detail = err.detail as { project_id?: string } | undefined;
          setLocalError({
            code: err.code,
            message: err.message,
            projectId: detail?.project_id,
          });
          return;
        }
      }
      setLocalError({
        code: "error",
        message: errorText(err),
      });
    },
  });

  const handleChoose = (path: string, initGit = false) => {
    if (addProjectMutation.isPending) return;
    setLocalError(null);
    addProjectMutation.mutate({ path, initGit });
  };

  const handleConfirmInitGit = () => {
    if (pendingInitPath) {
      setInitDialogOpen(false);
      handleChoose(pendingInitPath, true);
    }
  };

  return (
    <div className="space-y-4">
      {localError && (
        <div
          role="alert"
          className="flex flex-col gap-2 rounded-md bg-destructive/15 p-4 text-sm text-destructive sm:flex-row sm:items-center sm:justify-between"
        >
          <span>{localError.message}</span>
          <div className="flex shrink-0 items-center gap-2">
            {localError.code === "not_repo_root" && localError.repoRoot && (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  if (localError.repoRoot) {
                    handleChoose(localError.repoRoot, false);
                  }
                }}
              >
                Add {localError.repoRoot} instead
              </Button>
            )}
            {localError.code === "project_exists" && localError.projectId && (
              <Button asChild variant="outline" size="sm">
                <Link to={`/projects/${localError.projectId}`}>Open it</Link>
              </Button>
            )}
          </div>
        </div>
      )}

      <FolderBrowser
        onChoose={(path) => {
          handleChoose(path, false);
        }}
        isSubmitting={addProjectMutation.isPending}
        busyLabel="Adding project..."
      />

      <Dialog open={initDialogOpen} onOpenChange={setInitDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Initialize Git Repository</DialogTitle>
            <DialogDescription>{initDialogMessage}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={addProjectMutation.isPending}
              onClick={() => {
                setInitDialogOpen(false);
              }}
            >
              Cancel
            </Button>
            <Button
              type="button"
              disabled={addProjectMutation.isPending}
              onClick={handleConfirmInitGit}
            >
              Run git init
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
