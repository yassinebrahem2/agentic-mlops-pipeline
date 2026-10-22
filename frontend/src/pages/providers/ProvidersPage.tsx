import { errorText } from "@/lib/api";
import { useProviders } from "@/lib/useProviders";
import { AddModelDialog } from "./AddModelDialog";
import { AddProviderDialog } from "./AddProviderDialog";
import { AgentDefaults } from "./AgentDefaults";
import { DetectedKeys } from "./DetectedKeys";
import { ModelsTable } from "./ModelsTable";
import { ProviderCard } from "./ProviderCard";

export function ProvidersPage() {
  const providers = useProviders();

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-8 p-6">
      <h1 className="text-2xl font-semibold">AI providers</h1>

      <section aria-labelledby="providers-heading" className="flex flex-col gap-4">
        <div className="flex items-center justify-between gap-4">
          <h2 id="providers-heading" className="text-lg font-medium">
            Providers
          </h2>
          <AddProviderDialog />
        </div>
        {providers.isPending && <p className="text-muted-foreground">Loading providers…</p>}
        {providers.isError && <p className="text-destructive">{errorText(providers.error)}</p>}
        {providers.data?.length === 0 && (
          <p className="text-muted-foreground">
            No providers yet. Add one, or import a key found in the environment.
          </p>
        )}
        {providers.data?.map((provider) => (
          <ProviderCard key={provider.id} provider={provider} />
        ))}
      </section>

      <DetectedKeys />

      <section aria-labelledby="models-heading" className="flex flex-col gap-4">
        <div className="flex items-center justify-between gap-4">
          <h2 id="models-heading" className="text-lg font-medium">
            Models
          </h2>
          <AddModelDialog providers={providers.data ?? []} />
        </div>
        <ModelsTable />
      </section>

      <AgentDefaults />
    </div>
  );
}
