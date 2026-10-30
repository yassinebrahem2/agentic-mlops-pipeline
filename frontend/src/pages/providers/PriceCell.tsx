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
import { formatUsd } from "@/pages/usage/format";

type ModelEntry = components["schemas"]["ModelEntry"];

const PRICE_SOURCE_LABELS: Record<NonNullable<ModelEntry["price"]>["source"], string> = {
  provider: "From the provider",
  user: "Your price",
};

type PriceCellProps = { model: ModelEntry; onUpdated: (updated: ModelEntry) => void };

export function PriceCell({ model, onUpdated }: PriceCellProps) {
  const queryClient = useQueryClient();
  const clear = useMutation({
    mutationFn: async () =>
      unwrap(await api.PUT("/api/models/price", { body: { address: model.address, price: null } })),
    onSuccess: async (updated) => {
      onUpdated(updated);
      await queryClient.invalidateQueries({ queryKey: ["usage"] });
    },
  });
  const price = model.price;

  return (
    <div className="flex flex-col items-start gap-1">
      {price ? (
        <>
          <p>
            {formatUsd(price.prompt_per_mtok)} in / {formatUsd(price.completion_per_mtok)} out per
            1M tokens
          </p>
          <p className="text-muted-foreground">{PRICE_SOURCE_LABELS[price.source]}</p>
        </>
      ) : (
        <p>No price</p>
      )}
      <div className="flex gap-2">
        <SetPriceDialog model={model} onUpdated={onUpdated} />
        {price?.source === "user" && (
          <Button
            variant="outline"
            size="sm"
            disabled={clear.isPending}
            aria-label={`Clear price for ${model.address}`}
            onClick={() => {
              clear.mutate();
            }}
          >
            Clear
          </Button>
        )}
      </div>
      {clear.isError && <p className="text-destructive">{errorText(clear.error)}</p>}
    </div>
  );
}

function SetPriceDialog({ model, onUpdated }: PriceCellProps) {
  const queryClient = useQueryClient();
  const promptId = useId();
  const completionId = useId();
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [completion, setCompletion] = useState("");

  const save = useMutation({
    mutationFn: async (price: { prompt: number; completion: number }) =>
      unwrap(
        await api.PUT("/api/models/price", {
          body: {
            address: model.address,
            price: { prompt_per_mtok: price.prompt, completion_per_mtok: price.completion },
          },
        }),
      ),
    onSuccess: async (updated) => {
      onUpdated(updated);
      await queryClient.invalidateQueries({ queryKey: ["usage"] });
      setOpen(false);
    },
  });

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate({ prompt: Number(prompt), completion: Number(completion) });
  }

  const errors = fieldErrors(save.error);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setPrompt("");
          setCompletion("");
          save.reset();
        }
        setOpen(next);
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" aria-label={`Set price for ${model.address}`}>
          Set price
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Set price</DialogTitle>
          <DialogDescription>
            Enter what {model.address} costs in USD per million tokens. Your price is kept when the
            provider's list changes.
          </DialogDescription>
        </DialogHeader>
        <form noValidate onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor={promptId}>Prompt, USD per 1M tokens</Label>
            <Input
              id={promptId}
              type="number"
              min={0}
              step="any"
              inputMode="decimal"
              value={prompt}
              onChange={(event) => {
                setPrompt(event.target.value);
              }}
            />
            {errors.prompt_per_mtok !== undefined && (
              <p className="text-destructive">{errors.prompt_per_mtok}</p>
            )}
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor={completionId}>Completion, USD per 1M tokens</Label>
            <Input
              id={completionId}
              type="number"
              min={0}
              step="any"
              inputMode="decimal"
              value={completion}
              onChange={(event) => {
                setCompletion(event.target.value);
              }}
            />
            {errors.completion_per_mtok !== undefined && (
              <p className="text-destructive">{errors.completion_per_mtok}</p>
            )}
          </div>
          {save.isError && Object.keys(errors).length === 0 && (
            <p className="text-destructive">{errorText(save.error)}</p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={save.isPending || prompt === "" || completion === ""}>
              {save.isPending ? "Saving…" : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
