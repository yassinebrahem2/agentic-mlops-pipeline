import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { QueryStatus } from "@/components/QueryStatus";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorText, unwrap } from "@/lib/api";

type PublicUrlView = components["schemas"]["PublicUrlView"];

const publicUrlKey = ["integrations", "public-url"] as const;

export function PublicUrlSection() {
  const queryClient = useQueryClient();
  const view = useQuery({
    queryKey: publicUrlKey,
    queryFn: async () => unwrap(await api.GET("/api/integrations/public-url")),
  });
  const save = useMutation({
    mutationFn: async (public_url: string | null) =>
      unwrap(await api.PUT("/api/integrations/public-url", { body: { public_url } })),
    onSuccess: (saved) => {
      queryClient.setQueryData<PublicUrlView>(publicUrlKey, saved);
    },
  });

  return (
    <section aria-labelledby="public-url-heading">
      <Card>
        <CardHeader>
          <CardTitle>
            <h2 id="public-url-heading" className="text-lg font-medium">
              Public address
            </h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <QueryStatus query={view} loading="Loading the public URL..." />
          {view.data && (
            <PublicUrlForm
              // Keyed on the stored value so the field re-seeds after a save.
              key={view.data.public_url ?? ""}
              stored={view.data.public_url}
              pending={save.isPending}
              saved={save.isSuccess}
              error={save.error}
              onSave={(value) => {
                save.mutate(value);
              }}
            />
          )}
        </CardContent>
      </Card>
    </section>
  );
}

interface FormProps {
  stored: string | null;
  pending: boolean;
  saved: boolean;
  error: unknown;
  onSave: (value: string | null) => void;
}

function PublicUrlForm({ stored, pending, saved, error, onSave }: FormProps) {
  const inputId = useId();
  const helpId = useId();
  const [value, setValue] = useState(stored ?? "");
  const dirty = value !== (stored ?? "");

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = value.trim();
    onSave(trimmed === "" ? null : trimmed);
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-3">
      <div className="space-y-2">
        <Label htmlFor={inputId}>Public URL</Label>
        <Input
          id={inputId}
          className="w-full sm:w-96"
          aria-describedby={helpId}
          disabled={pending}
          value={value}
          onChange={(event) => {
            setValue(event.target.value);
          }}
        />
        <p id={helpId} className="text-sm text-muted-foreground">
          The address your phone reaches this app at. Notifications link to it. Leave it empty to
          clear it.
        </p>
      </div>
      {stored === null && (
        <p role="note" className="text-sm">
          No public URL is set, so links in notifications cannot be opened from a phone. Messages
          carry the full summary instead.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={pending || !dirty}>
          {pending ? "Saving..." : "Save public URL"}
        </Button>
        {saved && !dirty && <p role="status">Saved.</p>}
      </div>
      {error !== null && error !== undefined && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(error)}
        </p>
      )}
    </form>
  );
}
