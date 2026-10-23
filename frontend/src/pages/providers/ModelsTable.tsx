import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api, errorText, fieldErrors, unwrap } from "@/lib/api";
import { useModels } from "@/lib/useModels";

type ModelEntry = components["schemas"]["ModelEntry"];

const AVAILABILITY_LABELS: Record<ModelEntry["availability"], string> = {
  untested: "Untested",
  verified: "Verified",
  needs_attention: "Needs attention",
};

function storeModel(queryClient: QueryClient, model: ModelEntry): void {
  queryClient.setQueryData<ModelEntry[]>(["models"], (list) =>
    list?.map((m) => (m.address === model.address ? model : m)),
  );
}

export function ModelsTable() {
  const models = useModels();

  if (models.isPending) return <p className="text-muted-foreground">Loading models…</p>;
  if (models.isError) return <p className="text-destructive">{errorText(models.error)}</p>;
  if (models.data.length === 0) {
    return (
      <p className="text-muted-foreground">
        No models yet. Add a provider, then add one of its models.
      </p>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Address</TableHead>
          <TableHead>Tool calling</TableHead>
          <TableHead>Streaming</TableHead>
          <TableHead>Context window</TableHead>
          <TableHead>Availability</TableHead>
          <TableHead>Last verified</TableHead>
          <TableHead>Actions</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {models.data.map((model) => (
          <ModelRow key={model.address} model={model} />
        ))}
      </TableBody>
    </Table>
  );
}

function trait(model: ModelEntry, value: boolean): string {
  if (model.availability === "untested") return "Not checked";
  return value ? "Yes" : "No";
}

function ModelRow({ model }: { model: ModelEntry }) {
  const queryClient = useQueryClient();
  // Verification makes a real model call; the row shows its answer, or why it failed.
  const verify = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/models/verify", { body: { address: model.address } })),
    onSuccess: async (updated) => {
      storeModel(queryClient, updated);
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: async () => {
      // A failed check marks the model on the server, so reload it.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["models"] }),
        queryClient.invalidateQueries({ queryKey: ["agents"] }),
      ]);
    },
  });
  const remove = useMutation({
    mutationFn: async () => {
      unwrap(await api.DELETE("/api/models", { params: { query: { address: model.address } } }));
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["models"] }),
        queryClient.invalidateQueries({ queryKey: ["agents"] }),
      ]);
    },
  });

  return (
    <TableRow className="align-top">
      <TableCell className="font-mono">{model.address}</TableCell>
      <TableCell>{trait(model, model.tool_calling)}</TableCell>
      <TableCell>{trait(model, model.streaming)}</TableCell>
      <TableCell>
        {model.context_window === null ? (
          <ContextWindowForm address={model.address} />
        ) : (
          model.context_window
        )}
      </TableCell>
      <TableCell className="whitespace-normal">
        <p>{AVAILABILITY_LABELS[model.availability]}</p>
        {model.availability === "needs_attention" && model.availability_detail && (
          <p className="text-destructive">{model.availability_detail}</p>
        )}
      </TableCell>
      <TableCell>
        {model.verified_at ? new Date(model.verified_at).toLocaleString() : "Never"}
      </TableCell>
      <TableCell className="whitespace-normal">
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={verify.isPending}
            aria-label={`${verify.isPending ? "Verifying" : "Verify"} ${model.address}`}
            onClick={() => {
              verify.mutate();
            }}
          >
            {verify.isPending ? "Verifying…" : "Verify"}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={remove.isPending}
            aria-label={`Remove ${model.address}`}
            onClick={() => {
              remove.mutate();
            }}
          >
            Remove
          </Button>
        </div>
        {verify.isError && <p className="text-destructive">{errorText(verify.error)}</p>}
        {remove.isError && <p className="text-destructive">{errorText(remove.error)}</p>}
      </TableCell>
    </TableRow>
  );
}

function ContextWindowForm({ address }: { address: string }) {
  const queryClient = useQueryClient();
  const inputId = useId();
  const errorId = useId();
  const [value, setValue] = useState("");
  const save = useMutation({
    mutationFn: async (tokens: number) =>
      unwrap(await api.PUT("/api/models/context-window", { body: { address, value: tokens } })),
    onSuccess: async (updated) => {
      storeModel(queryClient, updated);
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate(Number(value));
  }

  const valueError = fieldErrors(save.error).value;

  return (
    // The server is the one judge of the value; its sentence shows under the input.
    <form noValidate onSubmit={submit} className="flex max-w-56 flex-col gap-2 whitespace-normal">
      <Label htmlFor={inputId}>Context window</Label>
      <div className="flex gap-2">
        <Input
          id={inputId}
          aria-invalid={valueError !== undefined ? true : undefined}
          aria-describedby={valueError !== undefined ? errorId : undefined}
          type="number"
          min={1}
          step={1}
          inputMode="numeric"
          value={value}
          onChange={(event) => {
            setValue(event.target.value);
          }}
        />
        <Button
          type="submit"
          size="sm"
          disabled={save.isPending || value === ""}
          aria-label={`Save context window of ${address}`}
        >
          Save
        </Button>
      </div>
      <p className="text-muted-foreground">
        This model cannot be assigned to an agent until its context window is set.
      </p>
      {valueError !== undefined ? (
        <p id={errorId} className="text-destructive">
          {valueError}
        </p>
      ) : (
        save.isError && <p className="text-destructive">{errorText(save.error)}</p>
      )}
    </form>
  );
}
