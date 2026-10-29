import { useState, type SyntheticEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { errorText } from "@/lib/api";
import { useSetSourceCredential } from "@/lib/useTracking";

export interface SourceCredentialFormProps {
  projectId: string;
  sourceId: string;
  initialError?: string | null;
  onSuccess?: () => void;
}

export function SourceCredentialForm({
  projectId,
  sourceId,
  initialError,
  onSuccess,
}: SourceCredentialFormProps) {
  const [credential, setCredential] = useState("");
  const [error, setError] = useState<string | null>(initialError ?? null);
  const setCredentialMutation = useSetSourceCredential(projectId);

  const handleSubmit = async (e: SyntheticEvent) => {
    e.preventDefault();
    if (!credential) return;

    const valueToSend = credential;
    // Clear credential from component state immediately so it is never left in DOM
    setCredential("");

    try {
      await setCredentialMutation.mutateAsync({ sourceId, credential: valueToSend });
      setError(null);
      onSuccess?.();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : errorText(err));
    }
  };

  const inputId = `credential-${sourceId}`;

  return (
    <form
      onSubmit={(e) => {
        void handleSubmit(e);
      }}
      className="space-y-2 pt-2"
    >
      <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-2">
        <Label htmlFor={inputId} className="text-sm font-medium">
          Credential
        </Label>
        <div className="flex items-center gap-2">
          <Input
            id={inputId}
            type="password"
            autoComplete="off"
            aria-label="Credential"
            placeholder="Enter credential"
            value={credential}
            onChange={(e) => {
              setCredential(e.target.value);
              setError(null);
            }}
            className="max-w-xs"
          />
          <Button
            type="submit"
            size="sm"
            disabled={!credential.trim() || setCredentialMutation.isPending}
          >
            Set
          </Button>
        </div>
      </div>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </form>
  );
}
