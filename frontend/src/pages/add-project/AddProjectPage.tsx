import { Loader2 } from "lucide-react";
import { GitHubTab } from "./GitHubTab";
import { LocalFolderTab } from "./LocalFolderTab";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errorText } from "@/lib/api";
import { useAppInfo } from "@/lib/useAppInfo";

export function AddProjectPage() {
  const { data: appInfo, isPending, error } = useAppInfo();

  const isServerMode = appInfo?.mode === "server";

  return (
    <div className="container mx-auto max-w-4xl space-y-6 p-6">
      <div className="space-y-1">
        <h1 className="text-2xl font-bold tracking-tight">Add project</h1>
        <p className="text-sm text-muted-foreground">
          {appInfo?.mode === "server" && "This server works with GitHub repositories."}
          {appInfo?.mode === "local" &&
            "Add a local repository or clone from GitHub to start working."}
        </p>
      </div>

      {isPending && (
        <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
          <Loader2 className="mr-2 size-4 animate-spin" />
          Loading...
        </div>
      )}

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {errorText(error)}
        </p>
      )}

      {!error && isServerMode && (
        <div className="space-y-4">
          <GitHubTab />
        </div>
      )}

      {!error && appInfo?.mode === "local" && (
        <Tabs defaultValue="local" className="space-y-4">
          <TabsList>
            <TabsTrigger value="local">Local folder</TabsTrigger>
            <TabsTrigger value="github">GitHub</TabsTrigger>
          </TabsList>
          <TabsContent value="local">
            <LocalFolderTab />
          </TabsContent>
          <TabsContent value="github">
            <GitHubTab />
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}
