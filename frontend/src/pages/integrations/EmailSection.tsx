import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { QueryStatus } from "@/components/QueryStatus";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { api, errorText, fieldErrors, unwrap } from "@/lib/api";
import { ChannelTestForm } from "./ChannelTestForm";

type EmailSettings = components["schemas"]["EmailSettings"];
type EmailUpdate = components["schemas"]["EmailUpdate"];
// Everything but the secret: the password never becomes a mutation variable.
type EmailFields = Omit<EmailUpdate, "password">;

const emailKey = ["integrations", "email"] as const;

const PORT_ERROR = "Enter a whole number.";

function parsePort(text: string): number | null {
  const trimmed = text.trim();
  if (!/^[0-9]+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return Number.isSafeInteger(value) ? value : null;
}

export function EmailSection() {
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: emailKey,
    queryFn: async () => unwrap(await api.GET("/api/integrations/email")),
  });
  // The password is a secret: it lives here only between the submit and the request, never in
  // the mutation's variables (TanStack keeps those in its cache after the request settles).
  const pendingPassword = useRef<string | null>(null);
  const save = useMutation({
    // A retry would find the secret already consumed and silently send null.
    retry: false,
    mutationFn: async (fields: EmailFields) => {
      const password = pendingPassword.current;
      pendingPassword.current = null;
      return unwrap(await api.PUT("/api/integrations/email", { body: { ...fields, password } }));
    },
    onSuccess: (saved) => {
      queryClient.setQueryData<EmailSettings>(emailKey, saved);
    },
  });
  const remove = useMutation({
    mutationFn: async () => {
      unwrap(await api.DELETE("/api/integrations/email/password"));
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: emailKey });
    },
  });

  return (
    <section aria-labelledby="email-heading">
      <Card>
        <CardHeader>
          <CardTitle>
            <h2 id="email-heading" className="text-lg font-medium">
              Email
            </h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <p className="text-sm text-muted-foreground">
            By email, waking someone is an urgent email. Whether it rings their phone depends on
            their phone settings for their mail app.
          </p>
          <QueryStatus query={settings} loading="Loading email settings..." />
          {settings.data && (
            <EmailForm
              // Keyed on the stored values so the form re-seeds after a save or a removal.
              key={JSON.stringify(settings.data)}
              stored={settings.data}
              pending={save.isPending || remove.isPending}
              saved={save.isSuccess}
              error={save.error ?? remove.error}
              onSave={(fields, password) => {
                pendingPassword.current = password.trim() === "" ? null : password;
                save.mutate(fields);
              }}
              onRemove={() => {
                save.reset();
                remove.mutate();
              }}
            />
          )}
          <ChannelTestForm
            channel="email"
            targetLabel="Address to send the test to"
            buttonLabel="Send test email"
            help="Sends a test email through the saved settings to this address."
          />
        </CardContent>
      </Card>
    </section>
  );
}

interface FormProps {
  stored: EmailSettings;
  pending: boolean;
  saved: boolean;
  error: unknown;
  onSave: (fields: EmailFields, password: string) => void;
  onRemove: () => void;
}

function EmailForm({ stored, pending, saved, error, onSave, onRemove }: FormProps) {
  const ids = { enabled: useId(), host: useId(), port: useId(), user: useId(), sender: useId() };
  const starttlsId = useId();
  const passwordId = useId();
  const passwordNoteId = useId();
  const [enabled, setEnabled] = useState(stored.enabled);
  const [host, setHost] = useState(stored.host);
  const [port, setPort] = useState(String(stored.port));
  const [user, setUser] = useState(stored.user);
  const [sender, setSender] = useState(stored.sender);
  const [starttls, setStarttls] = useState(stored.starttls);
  const [password, setPassword] = useState("");
  const [attempted, setAttempted] = useState(false);
  const parsedPort = parsePort(port);
  const dirty =
    enabled !== stored.enabled ||
    host !== stored.host ||
    parsedPort !== stored.port ||
    user !== stored.user ||
    sender !== stored.sender ||
    starttls !== stored.starttls ||
    password !== "";
  const serverErrors = fieldErrors(error);

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setAttempted(true);
    if (parsedPort === null) return;
    onSave({ enabled, host, port: parsedPort, user, sender, starttls }, password);
    setPassword(""); // sent once and never kept in the page
  }

  const textFields = [
    { name: "host", id: ids.host, label: "SMTP host", value: host, set: setHost },
    { name: "user", id: ids.user, label: "SMTP user", value: user, set: setUser },
    { name: "sender", id: ids.sender, label: "Sender address", value: sender, set: setSender },
  ];
  const portError = attempted && parsedPort === null ? PORT_ERROR : serverErrors.port;

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <Switch
          id={ids.enabled}
          aria-label="Send notifications by email"
          checked={enabled}
          disabled={pending}
          onCheckedChange={setEnabled}
        />
        <Label htmlFor={ids.enabled}>Send notifications by email</Label>
      </div>
      {textFields.map((field) => (
        <div key={field.name} className="space-y-2">
          <Label htmlFor={field.id}>{field.label}</Label>
          <Input
            id={field.id}
            className="w-full sm:w-96"
            disabled={pending}
            aria-invalid={serverErrors[field.name] !== undefined}
            value={field.value}
            onChange={(event) => {
              field.set(event.target.value);
            }}
          />
          {serverErrors[field.name] && (
            <p className="text-sm text-destructive">{serverErrors[field.name]}</p>
          )}
        </div>
      ))}
      <div className="space-y-2">
        <Label htmlFor={ids.port}>Port</Label>
        <Input
          id={ids.port}
          type="number"
          inputMode="numeric"
          className="w-full sm:w-40"
          disabled={pending}
          aria-invalid={portError !== undefined}
          value={port}
          onChange={(event) => {
            setPort(event.target.value);
          }}
        />
        {portError && <p className="text-sm text-destructive">{portError}</p>}
      </div>
      <div className="flex items-center gap-3">
        <Switch
          id={starttlsId}
          aria-label="Use STARTTLS"
          checked={starttls}
          disabled={pending}
          onCheckedChange={setStarttls}
        />
        <Label htmlFor={starttlsId}>Use STARTTLS</Label>
      </div>
      <div className="space-y-2">
        <Label htmlFor={passwordId}>SMTP password</Label>
        <Input
          id={passwordId}
          type="password"
          autoComplete="new-password"
          className="w-full sm:w-96"
          aria-describedby={passwordNoteId}
          disabled={pending}
          value={password}
          onChange={(event) => {
            setPassword(event.target.value);
          }}
        />
        <p id={passwordNoteId} className="text-sm text-muted-foreground">
          <span>{stored.password_set ? "A password is set." : "No password is set."}</span> The
          password is write-only: it is never shown again. Leave the field empty to keep the stored
          one.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" disabled={pending || !dirty}>
          {pending ? "Saving..." : "Save email"}
        </Button>
        {stored.password_set && (
          <Button type="button" variant="outline" disabled={pending} onClick={onRemove}>
            Remove password
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
