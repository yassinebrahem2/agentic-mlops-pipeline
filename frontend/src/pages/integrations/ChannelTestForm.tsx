import { useMutation } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorText, unwrap } from "@/lib/api";

type Channel = "discord" | "email";

interface Props {
  channel: Channel;
  targetLabel: string;
  buttonLabel: string;
  help: string;
}

/** Sends one test message through the saved settings and shows the server's scrubbed result. */
export function ChannelTestForm({ channel, targetLabel, buttonLabel, help }: Props) {
  const inputId = useId();
  const helpId = useId();
  const [target, setTarget] = useState("");
  const test = useMutation({
    mutationFn: async (value: string) =>
      unwrap(
        await api.POST("/api/integrations/{channel}/test", {
          params: { path: { channel } },
          body: { target: value },
        }),
      ),
  });

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    test.mutate(target.trim());
  }

  // A failed send still answers 200 with ok=false; only a failed request is an HTTP error.
  const failure = test.data && !test.data.ok ? (test.data.detail ?? "The test failed.") : null;
  return (
    <form onSubmit={submit} className="space-y-2 border-t pt-4">
      <Label htmlFor={inputId}>{targetLabel}</Label>
      <Input
        id={inputId}
        className="w-full sm:w-80"
        aria-describedby={helpId}
        value={target}
        onChange={(event) => {
          setTarget(event.target.value);
        }}
      />
      <p id={helpId} className="text-sm text-muted-foreground">
        {help}
      </p>
      <Button type="submit" variant="outline" disabled={test.isPending || target.trim() === ""}>
        {test.isPending ? "Sending..." : buttonLabel}
      </Button>
      {test.data?.ok && <p role="status">Test message sent.</p>}
      {failure !== null && (
        <p role="alert" className="text-sm text-destructive">
          {failure}
        </p>
      )}
      {test.isError && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(test.error)}
        </p>
      )}
    </form>
  );
}
