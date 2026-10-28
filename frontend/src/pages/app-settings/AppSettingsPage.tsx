import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { api, errorText, unwrap } from "@/lib/api";
import { useAppInfo } from "@/lib/useAppInfo";
import { EmbedderSection } from "./EmbedderSection";

type AppInfo = components["schemas"]["AppInfo"];

const MODE_LABELS: Record<AppInfo["mode"], string> = {
  local: "Local",
  server: "Server",
};

export function AppSettingsPage() {
  const app = useAppInfo();

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8 p-6">
      <h1 className="text-2xl font-semibold">App settings</h1>
      {app.isPending && <p className="text-muted-foreground">Loading…</p>}
      {app.isError && <p className="text-destructive">{errorText(app.error)}</p>}
      {app.data && (
        <>
          <AboutApp app={app.data} />
          <EmbedderSection />
          <Backup />
        </>
      )}
    </div>
  );
}

function AboutApp({ app }: { app: AppInfo }) {
  return (
    <section aria-labelledby="about-heading" className="flex flex-col gap-4">
      <h2 id="about-heading" className="text-lg font-medium">
        About
      </h2>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2">
        <dt className="text-muted-foreground">Name</dt>
        <dd>{app.name}</dd>
        <dt className="text-muted-foreground">Version</dt>
        <dd>{app.version}</dd>
        <dt className="text-muted-foreground">Run mode</dt>
        <dd>{MODE_LABELS[app.mode]}</dd>
        <dt className="text-muted-foreground">Data folder</dt>
        <dd className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-2">
            <code className="text-sm break-all">{app.data_dir}</code>
            <CopyButton text={app.data_dir} />
          </div>
        </dd>
      </dl>
    </section>
  );
}

function CopyButton({ text }: { text: string }) {
  const [result, setResult] = useState<"copied" | "failed" | null>(null);

  async function copy() {
    try {
      // navigator.clipboard is missing outside secure contexts, which makes this throw too.
      await navigator.clipboard.writeText(text);
      setResult("copied");
    } catch {
      setResult("failed");
    }
  }

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        aria-label="Copy data folder"
        onClick={() => {
          void copy();
        }}
      >
        Copy
      </Button>
      {/* The live region stays mounted so screen readers announce each new result. */}
      <span role="status" aria-label="Copy result" className="text-sm">
        {result === "copied" && <span className="text-muted-foreground">Copied.</span>}
        {result === "failed" && (
          <span className="text-destructive">
            Could not copy. Select the folder path and copy it by hand.
          </span>
        )}
      </span>
    </>
  );
}

function Backup() {
  const backup = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/app/backup")),
  });

  return (
    <section aria-labelledby="backup-heading" className="flex flex-col gap-4">
      <div>
        <h2 id="backup-heading" className="text-lg font-medium">
          Backup
        </h2>
        <p className="text-muted-foreground">
          Copies the app&apos;s databases into a new folder inside the data folder.
        </p>
      </div>
      <div>
        <Button
          disabled={backup.isPending}
          onClick={() => {
            backup.mutate();
          }}
        >
          {backup.isPending ? "Backing up…" : "Back up now"}
        </Button>
      </div>
      <div role="status" aria-label="Backup result">
        {backup.isSuccess && (
          <p>
            Backup written to <code className="text-sm break-all">{backup.data.path}</code>
          </p>
        )}
      </div>
      {backup.isError && <p className="text-destructive">{errorText(backup.error)}</p>}
    </section>
  );
}
