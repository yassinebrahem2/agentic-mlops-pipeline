import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorText, unwrap } from "@/lib/api";

type GitHubStatus = components["schemas"]["GitHubStatus"];

const CONNECTED_HOW: Record<Exclude<GitHubStatus["mode"], "none">, string> = {
  gh: "through the GitHub CLI",
  token: "with a pasted token",
};

function storeStatus(queryClient: QueryClient, status: GitHubStatus): void {
  queryClient.setQueryData<GitHubStatus>(["github"], status);
}

export function IntegrationsPage() {
  const github = useQuery({
    queryKey: ["github"],
    queryFn: async () => unwrap(await api.GET("/api/github")),
  });

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8 p-6">
      <h1 className="text-2xl font-semibold">Integrations</h1>
      <section aria-labelledby="github-heading">
        <Card>
          <CardHeader>
            <CardTitle>
              <h2 id="github-heading" className="text-lg font-medium">
                GitHub
              </h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            {github.isPending && <p className="text-muted-foreground">Loading…</p>}
            {github.isError && <p className="text-destructive">{errorText(github.error)}</p>}
            {github.data &&
              (github.data.mode === "none" ? (
                <NotConnected ghAvailable={github.data.gh_available} />
              ) : (
                <Connected login={github.data.login} how={CONNECTED_HOW[github.data.mode]} />
              ))}
          </CardContent>
        </Card>
      </section>
    </div>
  );
}

function Connected({ login, how }: { login: string | null; how: string }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-4">
      <div>
        <p className="font-medium">Connected as {login}</p>
        <p className="text-muted-foreground">{how}</p>
      </div>
      <DisconnectDialog />
    </div>
  );
}

function NotConnected({ ghAvailable }: { ghAvailable: boolean }) {
  return (
    <div className="flex flex-col gap-6">
      <p className="text-muted-foreground">
        Not connected. Connect GitHub to add projects from your repositories.
      </p>
      {ghAvailable && <GhLogin />}
      <TokenForm />
    </div>
  );
}

function GhLogin() {
  const queryClient = useQueryClient();
  const enable = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/github/gh")),
    onSuccess: (status) => {
      storeStatus(queryClient, status);
    },
  });

  return (
    <div className="flex flex-col gap-2">
      <div>
        <Button
          disabled={enable.isPending}
          onClick={() => {
            enable.mutate();
          }}
        >
          {enable.isPending ? "Checking GitHub CLI login…" : "Use my GitHub CLI login"}
        </Button>
      </div>
      {enable.isError && <p className="text-destructive">{errorText(enable.error)}</p>}
    </div>
  );
}

function TokenForm() {
  const queryClient = useQueryClient();
  const inputId = useId();
  const noteId = useId();
  const [token, setToken] = useState("");
  const connect = useMutation({
    mutationFn: async (value: string) =>
      unwrap(await api.POST("/api/github/token", { body: { token: value } })),
    onSuccess: (status) => {
      storeStatus(queryClient, status);
    },
  });

  function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    connect.mutate(token);
    setToken(""); // the token is sent once and never kept in the page
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-2">
      <Label htmlFor={inputId}>GitHub token</Label>
      <p id={noteId} className="text-muted-foreground text-sm">
        Paste a fine-grained personal access token. To read and clone your repositories it needs the
        repository permissions Contents: read and Metadata: read.
      </p>
      <Input
        id={inputId}
        type="password"
        autoComplete="off"
        aria-describedby={noteId}
        value={token}
        onChange={(event) => {
          setToken(event.target.value);
        }}
      />
      {connect.isError && <p className="text-destructive">{errorText(connect.error)}</p>}
      <div>
        <Button type="submit" disabled={connect.isPending || token.trim() === ""}>
          {connect.isPending ? "Checking token…" : "Connect"}
        </Button>
      </div>
    </form>
  );
}

function DisconnectDialog() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const disconnect = useMutation({
    mutationFn: async () => {
      unwrap(await api.DELETE("/api/github"));
    },
    onSuccess: async () => {
      setOpen(false);
      await queryClient.invalidateQueries({ queryKey: ["github"] });
    },
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        disconnect.reset();
        setOpen(next);
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline">Disconnect</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Disconnect GitHub?</DialogTitle>
          <DialogDescription>
            The app stops using your GitHub account until you connect again. A pasted token is
            deleted; your GitHub CLI login is not changed.
          </DialogDescription>
        </DialogHeader>
        {disconnect.isError && <p className="text-destructive">{errorText(disconnect.error)}</p>}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            disabled={disconnect.isPending}
            onClick={() => {
              disconnect.mutate();
            }}
          >
            Disconnect
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
