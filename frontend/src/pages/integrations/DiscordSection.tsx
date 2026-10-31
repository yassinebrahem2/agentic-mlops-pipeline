import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { QueryStatus } from "@/components/QueryStatus";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api, errorText, unwrap } from "@/lib/api";
import { ChannelTestForm } from "./ChannelTestForm";

type DiscordSettings = components["schemas"]["DiscordSettings"];

const discordKey = ["integrations", "discord"] as const;

export function DiscordSection() {
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: discordKey,
    queryFn: async () => unwrap(await api.GET("/api/integrations/discord")),
  });
  // The webhook URL is a secret: it lives here only between the submit and the request, never
  // in the mutation's variables (TanStack keeps those in its cache after the request settles).
  const pendingWebhook = useRef<string | null>(null);
  const save = useMutation({
    // A retry would find the secret already consumed and silently send null.
    retry: false,
    mutationFn: async (enabled: boolean) => {
      const webhook_url = pendingWebhook.current;
      pendingWebhook.current = null;
      return unwrap(await api.PUT("/api/integrations/discord", { body: { enabled, webhook_url } }));
    },
    onSuccess: (saved) => {
      queryClient.setQueryData<DiscordSettings>(discordKey, saved);
    },
  });
  const remove = useMutation({
    mutationFn: async () => {
      unwrap(await api.DELETE("/api/integrations/discord/webhook"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: discordKey });
    },
  });

  return (
    <section aria-labelledby="discord-heading">
      <Card>
        <CardHeader>
          <CardTitle>
            <h2 id="discord-heading" className="text-lg font-medium">
              Discord
            </h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <p className="text-sm text-muted-foreground">
            On Discord, waking someone is a mention. Whether it rings their phone depends on their
            phone settings for Discord.
          </p>
          <QueryStatus query={settings} loading="Loading Discord settings..." />
          {settings.data && (
            <DiscordForm
              // Keyed on the stored values so the form re-seeds after a save or a removal.
              key={JSON.stringify(settings.data)}
              stored={settings.data}
              pending={save.isPending || remove.isPending}
              saved={save.isSuccess}
              error={save.error ?? remove.error}
              onSave={(enabled, webhook) => {
                pendingWebhook.current = webhook.trim() === "" ? null : webhook;
                save.mutate(enabled);
              }}
              onRemove={() => {
                save.reset();
                remove.mutate();
              }}
            />
          )}
          <ChannelTestForm
            channel="discord"
            targetLabel="Discord user ID to mention"
            buttonLabel="Send test to Discord"
            help="Sends a test message through the saved settings, mentioning this user."
          />
        </CardContent>
      </Card>
    </section>
  );
}

interface FormProps {
  stored: DiscordSettings;
  pending: boolean;
  saved: boolean;
  error: unknown;
  onSave: (enabled: boolean, webhook: string) => void;
  onRemove: () => void;
}

function DiscordForm({ stored, pending, saved, error, onSave, onRemove }: FormProps) {
  const switchId = useId();
  const webhookId = useId();
  const stateId = useId();
  const [enabled, setEnabled] = useState(stored.enabled);
  const [webhook, setWebhook] = useState("");
  const dirty = enabled !== stored.enabled || webhook !== "";

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    onSave(enabled, webhook);
    setWebhook(""); // sent once and never kept in the page
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <Switch
          id={switchId}
          aria-label="Send notifications to Discord"
          checked={enabled}
          disabled={pending}
          onCheckedChange={setEnabled}
        />
        <Label htmlFor={switchId}>Send notifications to Discord</Label>
      </div>
      <div className="space-y-2">
        <Label htmlFor={webhookId}>Discord webhook URL</Label>
        <Input
          id={webhookId}
          type="password"
          autoComplete="off"
          className="w-full sm:w-96"
          aria-describedby={stateId}
          disabled={pending}
          value={webhook}
          onChange={(event) => {
            setWebhook(event.target.value);
          }}
        />
        <p id={stateId} className="text-sm text-muted-foreground">
          <span>{stored.webhook_set ? "A webhook is set." : "No webhook is set."}</span> The address
          is write-only: it is never shown again. Leave the field empty to keep the stored one.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={pending || !dirty}>
          {pending ? "Saving..." : "Save Discord"}
        </Button>
        {stored.webhook_set && (
          <Button type="button" variant="outline" disabled={pending} onClick={onRemove}>
            Remove webhook
          </Button>
        )}
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
