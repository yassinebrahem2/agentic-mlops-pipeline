import { useId } from "react";
import type { components } from "@/api/schema";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { hostedEmbedderWarning, usableModels } from "@/lib/models";
import { fromSelectValue, SELECT_NONE, toSelectValue } from "@/lib/selectNone";

type ModelEntry = components["schemas"]["ModelEntry"];
type Provider = components["schemas"]["Provider"];

export interface EmbedderSelectProps {
  value: string | null;
  onChange: (value: string | null) => void;
  models: ModelEntry[];
  providers: Provider[];
  noneLabel: string;
  label: string;
  id?: string;
  disabled?: boolean;
}

export function EmbedderSelect({
  value,
  onChange,
  models,
  providers,
  noneLabel,
  label,
  id,
  disabled,
}: EmbedderSelectProps) {
  const generatedId = useId();
  const selectId = id ?? generatedId;
  const usable = usableModels(models, "embedder");
  const isUnusable = value !== null && !usable.some((model) => model.address === value);

  const selectedModel = models.find((model) => model.address === value);
  const provider = providers.find((p) => p.name === selectedModel?.provider_name);

  return (
    <div className="space-y-2">
      <Label htmlFor={selectId}>{label}</Label>
      <Select
        value={toSelectValue(value)}
        disabled={disabled}
        onValueChange={(val) => {
          onChange(fromSelectValue(val));
        }}
      >
        <SelectTrigger id={selectId} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={SELECT_NONE}>{noneLabel}</SelectItem>
          {usable.map((entry) => (
            <SelectItem key={entry.address} value={entry.address}>
              {entry.address}
            </SelectItem>
          ))}
          {isUnusable && (
            <SelectItem key={value} value={value}>
              {value} (not usable)
            </SelectItem>
          )}
        </SelectContent>
      </Select>
      {provider?.kind === "hosted" && (
        <p className="text-sm text-amber-600 dark:text-amber-400">
          {hostedEmbedderWarning(provider.name)}
        </p>
      )}
    </div>
  );
}
