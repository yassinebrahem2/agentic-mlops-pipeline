import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { Alert, AlertDescription } from "@/components/ui/alert";
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

  const problems = models.data.map((m) => m.problem).filter((p): p is string => Boolean(p));

  return (
    <>
      {problems.length > 0 && (
        <Alert variant="warning">
          <AlertDescription>
            <ul className="list-inside list-disc">
              {problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
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
    </>
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
    <TableRow>
      <TableCell aria-label={model.address} className="font-mono">
        <div className="flex items-center gap-2">
          {model.problem && (
            <span
              role="img"
              aria-label={model.problem}
              className="size-2 shrink-0 rounded-full bg-amber-500"
            />
          )}
          <span>{model.address}</span>
        </div>
      </TableCell>
      <TableCell>{trait(model, model.tool_calling)}</TableCell>
      <TableCell>{trait(model, model.streaming)}</TableCell>
      <TableCell>
        {model.context_window === null ? (
          <SetContextWindowDialog model={model} />
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

function SetContextWindowDialog({ model }: { model: ModelEntry }) {
  const queryClient = useQueryClient();
  const inputId = useId();
  const errorId = useId();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [dirty, setDirty] = useState(false);

  const remote = useQuery({
    queryKey: ["remote-models", model.provider_name],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/providers/{name}/remote-models", {
          params: { path: { name: model.provider_name } },
        }),
      ),
    enabled: open && Boolean(model.provider_name),
  });

  useEffect(() => {
    if (!open) {
      setDirty(false);
      setValue("");
      return;
    }
    const matchId = (id: string) => id.replace(/^models\//, "");
    const listed = remote.data?.find(
      (m) => m.id === model.wire_model_id || matchId(m.id) === matchId(model.wire_model_id),
    )?.context_window;
    if (!dirty && listed !== null && listed !== undefined) {
      setValue(String(listed));
    }
  }, [open, remote.data, model.wire_model_id, dirty]);

  const save = useMutation({
    mutationFn: async (tokens: number) =>
      unwrap(
        await api.PUT("/api/models/context-window", {
          body: { address: model.address, value: tokens },
        }),
      ),
    onSuccess: async (updated) => {
      storeModel(queryClient, updated);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["models"] }),
        queryClient.invalidateQueries({ queryKey: ["agents"] }),
      ]);
      setOpen(false);
    },
  });

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate(Number(value));
  }

  const valueError = fieldErrors(save.error).value;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setDirty(false);
          setValue("");
          save.reset();
        }
        setOpen(next);
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          Set
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Set context window</DialogTitle>
          <DialogDescription>
            Enter the context window in tokens for {model.address}.
          </DialogDescription>
        </DialogHeader>
        <form noValidate onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor={inputId}>Context window</Label>
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
                setDirty(true);
                setValue(event.target.value);
              }}
            />
            {valueError !== undefined ? (
              <p id={errorId} className="text-destructive">
                {valueError}
              </p>
            ) : (
              save.isError && <p className="text-destructive">{errorText(save.error)}</p>
            )}
          </div>
          <DialogFooter>
            <Button type="submit" disabled={save.isPending || value === ""}>
              {save.isPending ? "Saving…" : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
