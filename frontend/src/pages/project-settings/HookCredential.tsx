import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type SubmitEvent } from "react";
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
import { api, unwrap } from "@/lib/api";
import { ActionError } from "@/pages/incidents/ActionError";
import { replaceHook, type Hook } from "./hooksCache";

/**
 * The credential is write-only: typed into a password field, sent once, cleared at once. As in
 * the email section it never becomes a mutation variable (TanStack keeps those in its cache), and
 * a retry would find it consumed, so there is none. Only `credential_set` comes back.
 */
export function HookCredential({ projectId, hook }: { projectId: string; hook: Hook }) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  const pendingCredential = useRef<string | null>(null);
  const params = { path: { project_id: projectId, hook_id: hook.id } };
  const inputId = `credential-${hook.id}`;

  const save = useMutation({
    retry: false,
    mutationFn: async () => {
      const credential = pendingCredential.current ?? "";
      pendingCredential.current = null;
      return unwrap(
        await api.PUT("/api/projects/{project_id}/hooks/{hook_id}/credential", {
          params,
          body: { credential },
        }),
      );
    },
    onSuccess: (saved) => {
      replaceHook(queryClient, projectId, saved);
    },
  });
  const remove = useMutation({
    mutationFn: async () =>
      unwrap(await api.DELETE("/api/projects/{project_id}/hooks/{hook_id}/credential", { params })),
    onSuccess: (saved) => {
      replaceHook(queryClient, projectId, saved);
    },
  });

  const submit = (event: SubmitEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (value === "" || save.isPending) return;
    pendingCredential.current = value;
    setValue("");
    save.mutate();
  };

  return (
    <div className="space-y-2">
      <p data-testid="hook-credential">
        {hook.credential_set ? "Credential set" : "No credential"}
      </p>
      <form className="flex flex-wrap items-end gap-2" onSubmit={submit}>
        <div className="space-y-1">
          <Label htmlFor={inputId}>Credential</Label>
          <Input
            id={inputId}
            type="password"
            autoComplete="new-password"
            className="w-full sm:w-64"
            disabled={save.isPending}
            value={value}
            onChange={(event) => {
              setValue(event.target.value);
            }}
          />
        </div>
        <Button type="submit" size="sm" disabled={value === "" || save.isPending}>
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
              <ActionError error={remove.error} />
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
      <ActionError error={save.error} />
    </div>
  );
}
