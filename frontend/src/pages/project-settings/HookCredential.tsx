import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type SubmitEvent } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorText, unwrap } from "@/lib/api";
import { replaceHook, type Hook } from "./hooksCache";

/**
 * The credential is write-only: it is typed into a password field, sent once, and cleared at once.
 * It never goes through useMutation, whose variables the cache keeps for minutes, so no query or
 * mutation entry, error text or rendered node can hold it. Only `credential_set` comes back.
 */
export function HookCredential({ projectId, hook }: { projectId: string; hook: Hook }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const params = { path: { project_id: projectId, hook_id: hook.id } };
  const inputId = `credential-${hook.id}`;

  const remove = useMutation({
    mutationFn: async () =>
      unwrap(await api.DELETE("/api/projects/{project_id}/hooks/{hook_id}/credential", { params })),
    onSuccess: (saved) => {
      replaceHook(queryClient, projectId, saved);
    },
  });

  const submit = async (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    const credential = value;
    if (credential === "" || pending) return;
    setValue("");
    setPending(true);
    setError(null);
    try {
      const saved = unwrap(
        await api.PUT("/api/projects/{project_id}/hooks/{hook_id}/credential", {
          params,
          body: { credential },
        }),
      );
      replaceHook(queryClient, projectId, saved);
    } catch (failure: unknown) {
      setError(failure);
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="space-y-2">
      <p data-testid="hook-credential">
        {hook.credential_set ? "Credential set" : "No credential"}
      </p>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          void submit(event);
        }}
      >
        <div className="space-y-1">
          <Label htmlFor={inputId}>Credential</Label>
          <Input
            id={inputId}
            type="password"
            autoComplete="new-password"
            className="w-full sm:w-64"
            disabled={pending}
            value={value}
            onChange={(event) => {
              setValue(event.target.value);
            }}
          />
        </div>
        <Button type="submit" size="sm" disabled={value === "" || pending}>
          Set credential
        </Button>
        {hook.credential_set && (
          <Dialog
            onOpenChange={() => {
              remove.reset();
            }}
          >
            <DialogTrigger asChild>
              <Button type="button" size="sm" variant="outline">
                Remove credential
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Remove this credential?</DialogTitle>
                <DialogDescription>
                  &ldquo;{hook.name}&rdquo; can no longer authenticate until a new credential is
                  set. The stored value cannot be recovered.
                </DialogDescription>
              </DialogHeader>
              {remove.isError && <p className="text-destructive">{errorText(remove.error)}</p>}
              <DialogFooter>
                <DialogClose asChild>
                  <Button variant="outline">Cancel</Button>
                </DialogClose>
                <Button
                  variant="destructive"
                  disabled={remove.isPending}
                  onClick={() => {
                    remove.mutate();
                  }}
                >
                  Remove credential
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        )}
      </form>
      {error !== null && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(error)}
        </p>
      )}
    </div>
  );
}
