import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
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

type Provider = components["schemas"]["Provider"];
type ModelCreate = components["schemas"]["ModelCreate"];

export function AddModelDialog({ providers }: { providers: Provider[] }) {
  const queryClient = useQueryClient();
  const providerId = useId();
  const filterId = useId();
  const modelIdId = useId();
  const contextId = useId();
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState("");
  const [filter, setFilter] = useState("");
  const [modelId, setModelId] = useState("");
  const [contextWindow, setContextWindow] = useState("");

  const remote = useQuery({
    queryKey: ["remote-models", provider],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/providers/{name}/remote-models", {
          params: { path: { name: provider } },
        }),
      ),
    enabled: open && provider !== "",
  });
  const add = useMutation({
    mutationFn: async (body: ModelCreate) => unwrap(await api.POST("/api/models", { body })),
    onSuccess: async () => {
      setOpen(false);
      reset();
      await queryClient.invalidateQueries({ queryKey: ["models"] });
    },
  });

  function reset() {
    setProvider("");
    setFilter("");
    setModelId("");
    setContextWindow("");
    add.reset();
  }

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const body: ModelCreate = { provider, wire_model_id: modelId.trim() };
    if (contextWindow !== "") body.context_window = Number(contextWindow);
    add.mutate(body);
  }

  const needle = filter.trim().toLowerCase();
  const shown = (remote.data ?? []).filter((m) => m.id.toLowerCase().includes(needle));
  const errors = fieldErrors(add.error);
  const hasFieldErrors = Object.keys(errors).length > 0;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        setOpen(next);
      }}
    >
      <DialogTrigger asChild>
        <Button disabled={providers.length === 0}>Add model</Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Add a model</DialogTitle>
          <DialogDescription>
            Choose one of the provider&apos;s models or type its model ID.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor={providerId}>Provider</Label>
            <select
              id={providerId}
              value={provider}
              className="h-9 rounded-md border border-input bg-transparent px-2.5 text-sm"
              onChange={(event) => {
                setProvider(event.target.value);
                setFilter("");
              }}
            >
              <option value="">Choose a provider</option>
              {providers.map((p) => (
                <option key={p.id} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>

          {provider !== "" && (
            <>
              {remote.isPending && (
                <p className="text-muted-foreground">Loading the provider&apos;s models…</p>
              )}
              {remote.isError && (
                <div className="text-destructive">
                  <p>{errorText(remote.error)}</p>
                  <p>You can still type a model ID below.</p>
                </div>
              )}
              {remote.data && (
                <div className="flex flex-col gap-2">
                  <Label htmlFor={filterId}>Filter models</Label>
                  <Input
                    id={filterId}
                    value={filter}
                    onChange={(event) => {
                      setFilter(event.target.value);
                    }}
                  />
                  <fieldset className="flex max-h-56 flex-col gap-1 overflow-y-auto rounded-md border p-2">
                    <legend className="sr-only">Models of {provider}</legend>
                    {shown.length === 0 && (
                      <p className="text-muted-foreground">No model matches.</p>
                    )}
                    {shown.map((m) => (
                      <label key={m.id} className="flex items-center gap-2 font-mono text-sm">
                        <input
                          type="radio"
                          name="remote-model"
                          value={m.id}
                          checked={modelId === m.id}
                          onChange={() => {
                            setModelId(m.id);
                          }}
                        />
                        {m.id}
                        {m.context_window !== null && (
                          <span className="text-muted-foreground">({m.context_window} tokens)</span>
                        )}
                      </label>
                    ))}
                  </fieldset>
                </div>
              )}
            </>
          )}

          <div className="flex flex-col gap-2">
            <Label htmlFor={modelIdId}>Model ID</Label>
            <Input
              id={modelIdId}
              value={modelId}
              onChange={(event) => {
                setModelId(event.target.value);
              }}
            />
            {errors.wire_model_id && <p className="text-destructive">{errors.wire_model_id}</p>}
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor={contextId}>Context window (optional)</Label>
            <Input
              id={contextId}
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              value={contextWindow}
              onChange={(event) => {
                setContextWindow(event.target.value);
              }}
            />
            <p className="text-muted-foreground">
              In tokens. Leave it empty to use the value the provider reports, if any.
            </p>
            {errors.context_window && <p className="text-destructive">{errors.context_window}</p>}
          </div>

          {add.isError && !hasFieldErrors && (
            <p className="text-destructive">{errorText(add.error)}</p>
          )}

          <DialogFooter>
            <Button
              type="submit"
              disabled={add.isPending || provider === "" || modelId.trim() === ""}
            >
              {add.isPending ? "Adding…" : "Add model"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
