import { useState, type SyntheticEvent } from "react";
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
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { errorText, fieldErrors } from "@/lib/api";
import {
  useCreateSource,
  useSetSourceCredential,
  type CreateSourceBody,
  type SourceType,
} from "@/lib/useTracking";

type MlflowSettings = components["schemas"]["MlflowSettings"];
type GithubActionsSettings = components["schemas"]["GithubActionsSettings"];

export interface AddTrackingSourceDialogProps {
  projectId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCredentialError?: (sourceId: string, error: string) => void;
}

export function AddTrackingSourceDialog({
  projectId,
  open,
  onOpenChange,
  onCredentialError,
}: AddTrackingSourceDialogProps) {
  const [type, setType] = useState<SourceType>("mlflow");
  const [url, setUrl] = useState("");
  const [needsCredential, setNeedsCredential] = useState(false);
  const [credential, setCredential] = useState("");
  const [repository, setRepository] = useState("");
  const [workflows, setWorkflows] = useState("");

  const createSource = useCreateSource(projectId);
  const setCredentialMutation = useSetSourceCredential(projectId);

  const resetForm = () => {
    setType("mlflow");
    setUrl("");
    setNeedsCredential(false);
    setCredential("");
    setRepository("");
    setWorkflows("");
    createSource.reset();
  };

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      resetForm();
    }
    onOpenChange(nextOpen);
  };

  const isSubmitting = createSource.isPending || setCredentialMutation.isPending;

  const canSubmit = type === "mlflow" ? url.trim().length > 0 : repository.trim().length > 0;

  const handleSubmit = async (e: SyntheticEvent) => {
    e.preventDefault();
    if (!canSubmit || isSubmitting) return;

    if (type === "mlflow") {
      const settings: MlflowSettings = { url: url.trim() };
      const body: CreateSourceBody = {
        type: "mlflow",
        settings,
        auth: needsCredential ? "credential" : "none",
      };

      try {
        const created = await createSource.mutateAsync(body);
        if (needsCredential && credential) {
          const credToSend = credential;
          setCredential("");
          try {
            await setCredentialMutation.mutateAsync({
              sourceId: created.id,
              credential: credToSend,
            });
          } catch (credErr: unknown) {
            const msg = credErr instanceof Error ? credErr.message : errorText(credErr);
            onCredentialError?.(created.id, msg);
          } finally {
            setCredentialMutation.reset();
          }
        }
        handleOpenChange(false);
      } catch {
        // Keep dialog open on 422 or error
      }
    } else {
      const workflowList = workflows
        .split(",")
        .map((w) => w.trim())
        .filter(Boolean);
      const settings: GithubActionsSettings = {
        repository: repository.trim(),
        workflows: workflowList,
      };
      const body: CreateSourceBody = {
        type: "github_actions",
        settings,
        auth: "none",
      };

      try {
        await createSource.mutateAsync(body);
        handleOpenChange(false);
      } catch {
        // Keep dialog open on 422 or error
      }
    }
  };

  const fieldErrs = fieldErrors(createSource.error);

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Add tracking source</DialogTitle>
          <DialogDescription>
            Connect an experiment or run tracking source to this project.
          </DialogDescription>
        </DialogHeader>

        <form
          onSubmit={(e) => {
            void handleSubmit(e);
          }}
          className="space-y-4"
        >
          <div className="space-y-2">
            <Label htmlFor="source-type">Type</Label>
            <Select
              value={type}
              onValueChange={(val) => {
                setType(val as SourceType);
                createSource.reset();
              }}
            >
              <SelectTrigger id="source-type" aria-label="Type" className="w-full">
                <SelectValue placeholder="Select type" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="mlflow">MLflow</SelectItem>
                <SelectItem value="github_actions">GitHub Actions</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {type === "mlflow" && (
            <>
              <div className="space-y-2">
                <Label htmlFor="mlflow-url">URL</Label>
                <Input
                  id="mlflow-url"
                  placeholder="https://mlflow.example.com"
                  value={url}
                  onChange={(e) => {
                    setUrl(e.target.value);
                  }}
                  disabled={isSubmitting}
                />
                {(fieldErrs["url"] || fieldErrs["settings.url"]) && (
                  <p className="text-sm text-destructive">
                    {fieldErrs["url"] || fieldErrs["settings.url"]}
                  </p>
                )}
              </div>

              <div className="flex items-center space-x-2">
                <Switch
                  id="needs-credential"
                  checked={needsCredential}
                  onCheckedChange={setNeedsCredential}
                  disabled={isSubmitting}
                />
                <Label htmlFor="needs-credential">Needs a credential</Label>
              </div>

              {needsCredential && (
                <div className="space-y-2">
                  <Label htmlFor="mlflow-credential">Credential</Label>
                  <Input
                    id="mlflow-credential"
                    type="password"
                    autoComplete="off"
                    aria-label="Credential"
                    placeholder="Enter credential"
                    value={credential}
                    onChange={(e) => {
                      setCredential(e.target.value);
                    }}
                    disabled={isSubmitting}
                  />
                  {(fieldErrs["credential"] || fieldErrs["settings.credential"]) && (
                    <p className="text-sm text-destructive">
                      {fieldErrs["credential"] || fieldErrs["settings.credential"]}
                    </p>
                  )}
                </div>
              )}
            </>
          )}

          {type === "github_actions" && (
            <>
              <div className="space-y-2">
                <Label htmlFor="github-repo">Repository</Label>
                <Input
                  id="github-repo"
                  placeholder="owner/repo"
                  value={repository}
                  onChange={(e) => {
                    setRepository(e.target.value);
                  }}
                  disabled={isSubmitting}
                />
                {(fieldErrs["repository"] || fieldErrs["settings.repository"]) && (
                  <p className="text-sm text-destructive">
                    {fieldErrs["repository"] || fieldErrs["settings.repository"]}
                  </p>
                )}
              </div>

              <div className="space-y-2">
                <Label htmlFor="github-workflows">Workflows</Label>
                <Input
                  id="github-workflows"
                  placeholder="ci.yml, release.yml"
                  value={workflows}
                  onChange={(e) => {
                    setWorkflows(e.target.value);
                  }}
                  disabled={isSubmitting}
                />
                {(fieldErrs["workflows"] || fieldErrs["settings.workflows"]) && (
                  <p className="text-sm text-destructive">
                    {fieldErrs["workflows"] || fieldErrs["settings.workflows"]}
                  </p>
                )}
              </div>

              <p className="text-sm text-muted-foreground">Uses the app&apos;s GitHub login.</p>
            </>
          )}

          {createSource.error && (
            <p role="alert" className="text-sm text-destructive">
              {errorText(createSource.error)}
            </p>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={isSubmitting}
              onClick={() => {
                handleOpenChange(false);
              }}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={!canSubmit || isSubmitting}>
              Add
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
