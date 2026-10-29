import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, unwrap } from "@/lib/api";
import { proposalByApprovalKey } from "./useProposal";

type SlotState = components["schemas"]["SlotState"];

export interface CredentialFieldProps {
  projectId: string;
  proposalId: string;
  approvalId: string;
  slot: SlotState;
  isPending: boolean;
}

export function CredentialField({
  projectId,
  proposalId,
  approvalId,
  slot,
  isPending,
}: CredentialFieldProps) {
  const queryClient = useQueryClient();
  const [value, setValue] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [locallySet, setLocallySet] = useState<boolean | null>(null);

  const isSet = locallySet !== null ? locallySet : slot.set;
  const inputId = `credential-slot-${String(slot.index)}`;

  const handleSave = async () => {
    if (!value || !isPending || isSaving) return;

    const secret = value;
    // Clears local state at once (also on error)
    setValue("");
    setError(null);
    setIsSaving(true);

    try {
      const updated = unwrap(
        await api.PUT("/api/projects/{project_id}/proposals/{proposal_id}/credentials/{slot}", {
          params: {
            path: {
              project_id: projectId,
              proposal_id: proposalId,
              slot: slot.index,
            },
          },
          body: { value: secret },
        }),
      );
      setLocallySet(updated.set);
      await queryClient.invalidateQueries({
        queryKey: proposalByApprovalKey(projectId, approvalId),
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save credential");
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="space-y-1.5 rounded-md border border-border/60 bg-muted/20 p-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Label htmlFor={inputId} className="text-xs font-semibold text-foreground">
          {slot.label}
        </Label>
        <span className="text-xs text-muted-foreground">
          credential set: {isSet ? "yes" : "no"}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <Input
          id={inputId}
          type="password"
          autoComplete="off"
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
          }}
          disabled={!isPending || isSaving}
          placeholder="Enter secret..."
          className="h-8 text-xs font-mono"
        />
        <Button
          type="button"
          size="sm"
          variant="secondary"
          onClick={() => {
            void handleSave();
          }}
          disabled={!isPending || value.length === 0 || isSaving}
          className="h-8 px-3 text-xs shrink-0"
        >
          {isSaving ? "Saving..." : "Save"}
        </Button>
      </div>

      {!isSet && (
        <p className="text-[11px] text-muted-foreground">
          the source is created without a credential; add one under Tracking sources in the project
          settings
        </p>
      )}

      {error && (
        <div role="alert" className="text-xs text-destructive">
          {error}
        </div>
      )}
    </div>
  );
}
