import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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

type Provider = components["schemas"]["Provider"];

const KIND_LABELS: Record<Provider["kind"], string> = {
  hosted: "Hosted",
  self_hosted: "Self-hosted",
};

// Key positions shift after a removal, so the card always renders the provider the server returned.
function storeProvider(queryClient: QueryClient, provider: Provider): void {
  queryClient.setQueryData<Provider[]>(["providers"], (list) =>
    list?.map((p) => (p.id === provider.id ? provider : p)),
  );
}

export function ProviderCard({ provider }: { provider: Provider }) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId}>
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2">
            <h3 id={headingId}>{provider.name}</h3>
            <Badge variant="secondary">{KIND_LABELS[provider.kind]}</Badge>
          </CardTitle>
          <p className="text-muted-foreground break-all">{provider.base_url}</p>
        </CardHeader>
        <CardContent>
          {provider.status === "needs_attention" && (
            <Alert variant="destructive">
              <AlertDescription>
                <p>{provider.status_detail}</p>
                <p>Fix the key below.</p>
              </AlertDescription>
            </Alert>
          )}
          <KeyList provider={provider} />
          <AddKeyForm provider={provider} />
          <div>
            <RemoveProviderDialog name={provider.name} />
          </div>
        </CardContent>
      </Card>
    </section>
  );
}

function KeyList({ provider }: { provider: Provider }) {
  const queryClient = useQueryClient();
  const removeKey = useMutation({
    mutationFn: async (position: number) =>
      unwrap(
        await api.DELETE("/api/providers/{name}/keys/{position}", {
          params: { path: { name: provider.name, position } },
        }),
      ),
    onSuccess: (updated) => {
      storeProvider(queryClient, updated);
    },
  });

  if (provider.keys.length === 0) {
    return (
      <p className="text-muted-foreground">
        {provider.kind === "hosted"
          ? "No keys yet."
          : "No key. A self-hosted server may not need one."}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <ul className="flex flex-col gap-2">
        {provider.keys.map((key) => (
          <li key={key.position} className="flex items-center justify-between gap-2">
            <code className="text-sm">{key.hint}</code>
            <Button
              variant="outline"
              size="sm"
              disabled={removeKey.isPending}
              aria-label={`Remove key ${key.hint}`}
              onClick={() => {
                removeKey.mutate(key.position);
              }}
            >
              Remove
            </Button>
          </li>
        ))}
      </ul>
      {removeKey.isError && <p className="text-destructive">{errorText(removeKey.error)}</p>}
    </div>
  );
}

function AddKeyForm({ provider }: { provider: Provider }) {
  const queryClient = useQueryClient();
  const inputId = useId();
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const addKey = useMutation({
    mutationFn: async (value: string) =>
      unwrap(
        await api.POST("/api/providers/{name}/keys", {
          params: { path: { name: provider.name } },
          body: { key: value },
        }),
      ),
    onSuccess: (updated) => {
      storeProvider(queryClient, updated);
      setOpen(false);
    },
  });

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    addKey.mutate(key);
    setKey(""); // the key is sent once and never kept in the page
  }

  if (!open) {
    return (
      <div>
        <Button
          variant="outline"
          onClick={() => {
            addKey.reset();
            setOpen(true);
          }}
        >
          Add key
        </Button>
      </div>
    );
  }
  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      <Label htmlFor={inputId}>New key for {provider.name}</Label>
      <Input
        id={inputId}
        type="password"
        autoComplete="off"
        value={key}
        onChange={(event) => {
          setKey(event.target.value);
        }}
      />
      {addKey.isError && <p className="text-destructive">{errorText(addKey.error)}</p>}
      <div className="flex gap-2">
        <Button type="submit" disabled={addKey.isPending || key.trim() === ""}>
          {addKey.isPending ? "Checking key…" : "Save key"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          onClick={() => {
            setKey("");
            setOpen(false);
          }}
        >
          Cancel
        </Button>
      </div>
    </form>
  );
}

function RemoveProviderDialog({ name }: { name: string }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const remove = useMutation({
    mutationFn: async () => {
      unwrap(await api.DELETE("/api/providers/{name}", { params: { path: { name } } }));
    },
    onSuccess: async () => {
      setOpen(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["providers"] }),
        queryClient.invalidateQueries({ queryKey: ["models"] }),
        queryClient.invalidateQueries({ queryKey: ["env-keys"] }),
      ]);
    },
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        remove.reset();
        setOpen(next);
      }}
    >
      <DialogTrigger asChild>
        <Button variant="destructive" aria-label={`Remove provider ${name}`}>
          Remove provider
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Remove {name}?</DialogTitle>
          <DialogDescription>
            Its saved keys are deleted and its models leave the list. Agents that use one of its
            models need another model.
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
            Remove provider
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
