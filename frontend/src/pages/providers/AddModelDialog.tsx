import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api, errorText, fieldErrors, unwrap } from "@/lib/api";
import { useRemoteModels } from "@/lib/useRemoteModels";

type Provider = components["schemas"]["Provider"];
type ModelCreate = components["schemas"]["ModelCreate"];

export function AddModelDialog({ providers }: { providers: Provider[] }) {
  const queryClient = useQueryClient();
  const providerId = useId();
  const modelIdId = useId();
  const contextId = useId();
  const [open, setOpen] = useState(false);
  const [provider, setProvider] = useState("");
  const [modelId, setModelId] = useState("");
  const [contextWindow, setContextWindow] = useState("");

  const remote = useRemoteModels(provider, { enabled: open });
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

  const options = (remote.data ?? []).map((m) => ({
    value: m.id,
    label: m.id,
    detail: m.context_window !== null ? `${String(m.context_window)} tokens` : undefined,
  }));
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
            <Select
              value={provider}
              onValueChange={(value) => {
                setProvider(value);
              }}
            >
              <SelectTrigger id={providerId} className="w-full">
                <SelectValue placeholder="Choose a provider" />
              </SelectTrigger>
              <SelectContent>
                {providers.map((p) => (
                  <SelectItem key={p.id} value={p.name}>
                    {p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {provider !== "" && remote.isError && (
            <div className="text-destructive">
              <p>{errorText(remote.error)}</p>
              <p>You can still type a model ID below.</p>
            </div>
          )}

          <div className="flex flex-col gap-2">
            <Label htmlFor={modelIdId}>Model</Label>
            <Combobox
              id={modelIdId}
              label="Model"
              value={modelId}
              onValueChange={setModelId}
              options={options}
              loading={remote.isPending && provider !== ""}
              placeholder="Choose or type a model ID"
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
