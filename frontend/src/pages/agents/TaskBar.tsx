import { useState } from "react";
import { useParams } from "react-router";
import type { Client } from "openapi-fetch";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api, errorText, unwrap, type paths as GeneratedPaths } from "@/lib/api";
import { cn } from "@/lib/utils";

export type TaskView = components["schemas"]["TaskView"];

export type TaskDiscardPaths = GeneratedPaths & {
  "/api/projects/{project_id}/tasks/{task_id}/discard": {
    post: {
      parameters: {
        path: {
          project_id: string;
          task_id: string;
        };
      };
      responses: {
        204: {
          content: never;
        };
      };
    };
  };
};

export interface TaskBarProps {
  task: TaskView;
  projectId?: string;
  onDiscard?: () => void;
  className?: string;
}

export function TaskBar({ task, projectId, onDiscard, className }: TaskBarProps) {
  const routeParams = useParams<{ projectId: string }>();
  const activeProjectId = projectId ?? routeParams.projectId ?? "";

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDiscard = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const client = api as unknown as Client<TaskDiscardPaths>;
      const res = await client.POST("/api/projects/{project_id}/tasks/{task_id}/discard", {
        params: {
          path: {
            project_id: activeProjectId,
            task_id: task.id,
          },
        },
      });
      unwrap(res);
      setConfirmOpen(false);
      onDiscard?.();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <div
        role="region"
        aria-label="Current task"
        className={cn(
          "flex items-center justify-between gap-4 border-t border-b bg-muted/30 px-4 py-2 text-xs",
          className,
        )}
      >
        <div className="flex flex-wrap items-center gap-2 min-w-0">
          <span className="font-medium text-foreground truncate">{task.title}</span>
          <code className="rounded bg-muted px-1.5 py-0.5 text-muted-foreground font-mono">
            {task.branch}
          </code>
          {task.pr_url ? (
            <a
              href={task.pr_url}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline hover:opacity-80 font-medium"
            >
              PR {task.pr_number ? `#${String(task.pr_number)}` : "link"}
            </a>
          ) : (
            <span className="text-muted-foreground">local review</span>
          )}
        </div>

        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            setError(null);
            setConfirmOpen(true);
          }}
          className="text-destructive hover:bg-destructive/10 hover:text-destructive h-7 text-xs shrink-0"
        >
          Discard task
        </Button>
      </div>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Discard task</DialogTitle>
            <DialogDescription>
              Are you sure you want to discard this task? All unpublished changes on this branch
              will be lost.
            </DialogDescription>
          </DialogHeader>

          {error && (
            <div role="alert" className="text-xs text-destructive">
              {error}
            </div>
          )}

          <DialogFooter className="gap-2 sm:gap-0">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                setConfirmOpen(false);
              }}
              disabled={submitting}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              size="sm"
              onClick={() => {
                void handleDiscard();
              }}
              disabled={submitting}
            >
              Discard
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
