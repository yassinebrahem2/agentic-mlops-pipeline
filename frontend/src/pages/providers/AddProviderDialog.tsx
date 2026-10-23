import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorText, fieldErrors, unwrap } from "@/lib/api";

type ProviderCreate = components["schemas"]["ProviderCreate"];
type Kind = ProviderCreate["kind"];

const KINDS: { value: Kind; label: string }[] = [
  { value: "hosted", label: "Hosted" },
  { value: "self_hosted", label: "Self-hosted" },
];

export function AddProviderDialog() {
  const queryClient = useQueryClient();
  const nameId = useId();
  const nameHintId = useId();
  const nameErrorId = useId();
  const baseUrlId = useId();
  const baseUrlHintId = useId();
  const baseUrlErrorId = useId();
  const keyId = useId();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Kind>("hosted");
  const [baseUrl, setBaseUrl] = useState("");
  const [firstKey, setFirstKey] = useState("");
  // Set once the provider exists, so a retry after a rejected key only sends the key.
  const [created, setCreated] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: async (body: ProviderCreate) => unwrap(await api.POST("/api/providers", { body })),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["providers"] }),
        queryClient.invalidateQueries({ queryKey: ["env-keys"] }),
      ]);
    },
  });
  const addKey = useMutation({
    mutationFn: async (vars: { name: string; key: string }) =>
      unwrap(
        await api.POST("/api/providers/{name}/keys", {
          params: { path: { name: vars.name } },
          body: { key: vars.key },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["providers"] });
    },
  });

  function reset() {
    setName("");
    setKind("hosted");
    setBaseUrl("");
    setFirstKey("");
    setCreated(null);
    create.reset();
    addKey.reset();
  }

  function finish() {
    setOpen(false);
    reset();
  }

  function sendKey(providerName: string, key: string) {
    if (key === "") {
      finish();
      return;
    }
    addKey.mutate({ name: providerName, key }, { onSuccess: finish });
  }

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const key = kind === "hosted" ? firstKey.trim() : "";
    setFirstKey(""); // the key is sent once and never kept in the page
    if (created !== null) {
      sendKey(created, key);
      return;
    }
    create.mutate(
      { name, kind, base_url: baseUrl },
      {
        onSuccess: (provider) => {
          setCreated(provider.name);
          sendKey(provider.name, key);
        },
      },
    );
  }

  const errors = fieldErrors(create.error);
  const hasFieldErrors = Object.keys(errors).length > 0;
  const pending = create.isPending || addKey.isPending;
  const describedBy = (...parts: (string | false)[]) =>
    parts.filter((p): p is string => p !== false).join(" ") || undefined;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        setOpen(next);
      }}
    >
      <DialogTrigger asChild>
        <Button>Add provider</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add a provider</DialogTitle>
          <DialogDescription>
            Any server that speaks the OpenAI-compatible protocol: a hosted API or one you run.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor={nameId}>Name</Label>
            <Input
              id={nameId}
              value={name}
              disabled={created !== null}
              aria-invalid={errors.name ? true : undefined}
              aria-describedby={describedBy(nameHintId, errors.name !== undefined && nameErrorId)}
              onChange={(event) => {
                setName(event.target.value);
              }}
            />
            <p id={nameHintId} className="text-muted-foreground">
              Lowercase letters, digits and dashes. Model addresses start with it.
            </p>
            {errors.name && (
              <p id={nameErrorId} className="text-destructive">
                {errors.name}
              </p>
            )}
          </div>

          <fieldset className="flex flex-col gap-2" disabled={created !== null}>
            <legend className="mb-2 text-sm font-medium">Kind</legend>
            <div className="flex gap-4">
              {KINDS.map((option) => (
                <label key={option.value} className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="kind"
                    value={option.value}
                    checked={kind === option.value}
                    onChange={() => {
                      setKind(option.value);
                    }}
                  />
                  {option.label}
                </label>
              ))}
            </div>
            {errors.kind && <p className="text-destructive">{errors.kind}</p>}
          </fieldset>

          <div className="flex flex-col gap-2">
            <Label htmlFor={baseUrlId}>Base URL</Label>
            <Input
              id={baseUrlId}
              value={baseUrl}
              disabled={created !== null}
              aria-invalid={errors.base_url ? true : undefined}
              aria-describedby={describedBy(
                kind === "self_hosted" && baseUrlHintId,
                errors.base_url !== undefined && baseUrlErrorId,
              )}
              onChange={(event) => {
                setBaseUrl(event.target.value);
              }}
            />
            {kind === "self_hosted" && (
              <p id={baseUrlHintId} className="text-muted-foreground">
                for example http://localhost:11434/v1 for Ollama
              </p>
            )}
            {errors.base_url && (
              <p id={baseUrlErrorId} className="text-destructive">
                {errors.base_url}
              </p>
            )}
          </div>

          {kind === "hosted" && (
            <div className="flex flex-col gap-2">
              <Label htmlFor={keyId}>First key (optional)</Label>
              <Input
                id={keyId}
                type="password"
                autoComplete="off"
                value={firstKey}
                onChange={(event) => {
                  setFirstKey(event.target.value);
                }}
              />
            </div>
          )}

          {create.isError && !hasFieldErrors && (
            <p className="text-destructive">{errorText(create.error)}</p>
          )}
          {addKey.isError && created !== null && (
            <div className="text-destructive">
              <p>{errorText(addKey.error)}</p>
              <p>
                {created} was added without a key. Paste another key, or close this and add one
                later.
              </p>
            </div>
          )}

          <DialogFooter>
            <Button type="submit" disabled={pending}>
              {pending ? "Adding…" : "Add provider"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
