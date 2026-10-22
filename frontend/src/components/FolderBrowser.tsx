import { useQuery } from "@tanstack/react-query";
import { ArrowUp, Folder, HardDrive, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, errorText, unwrap } from "@/lib/api";

export interface FolderBrowserProps {
  initialPath?: string;
  onChoose: (path: string) => void;
  isSubmitting?: boolean;
  busyLabel?: string;
}

export function FolderBrowser({
  initialPath,
  onChoose,
  isSubmitting,
  busyLabel,
}: FolderBrowserProps) {
  const [currentPath, setCurrentPath] = useState<string | undefined>(initialPath);
  const [folderPathInput, setFolderPathInput] = useState<string>(initialPath ?? "");

  const {
    data: listing,
    error,
    isPending,
    isError,
  } = useQuery({
    queryKey: ["folders", currentPath],
    queryFn: async () => {
      const res = await api.GET("/api/folders", {
        params: {
          query: currentPath ? { path: currentPath } : {},
        },
      });
      return unwrap(res);
    },
  });

  useEffect(() => {
    if (listing?.path) {
      setFolderPathInput(listing.path);
    }
  }, [listing?.path]);

  const handleGo = () => {
    const trimmed = folderPathInput.trim();
    if (trimmed) {
      setCurrentPath(trimmed);
    }
  };

  return (
    <div className="space-y-4 rounded-lg border p-4">
      <div className="flex items-center gap-2">
        <Label htmlFor="folder-path-input" className="shrink-0 text-sm font-medium">
          Folder path
        </Label>
        <Input
          id="folder-path-input"
          aria-label="Folder path"
          value={folderPathInput}
          onChange={(e) => {
            setFolderPathInput(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              handleGo();
            }
          }}
          placeholder="/path/to/folder"
          className="flex-1"
        />
        <Button type="button" onClick={handleGo}>
          Go
        </Button>
      </div>

      {isError && (
        <div role="alert" className="rounded-md bg-destructive/15 p-3 text-sm text-destructive">
          {errorText(error)}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-b pb-3">
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!listing?.parent || isPending}
            onClick={() => {
              if (listing?.parent) {
                setCurrentPath(listing.parent);
              }
            }}
          >
            <ArrowUp className="mr-1 size-3.5" />
            Up
          </Button>

          {listing?.roots && listing.roots.length > 1 && (
            <div className="flex items-center gap-1 pl-2">
              <HardDrive className="size-3.5 text-muted-foreground" />
              {listing.roots.map((root) => (
                <Button
                  key={root}
                  type="button"
                  variant={listing.path.startsWith(root) ? "secondary" : "outline"}
                  size="sm"
                  className="h-8 px-2 text-xs"
                  onClick={() => {
                    setCurrentPath(root);
                  }}
                >
                  {root}
                </Button>
              ))}
            </div>
          )}
        </div>

        <Button
          type="button"
          disabled={!listing?.path || isPending || isSubmitting}
          onClick={() => {
            if (listing?.path) {
              onChoose(listing.path);
            }
          }}
        >
          {isSubmitting ? (
            <>
              <Loader2 className="mr-1.5 size-4 animate-spin" />
              {busyLabel ?? "Use this folder"}
            </>
          ) : (
            "Use this folder"
          )}
        </Button>
      </div>

      {listing?.path && (
        <div className="text-sm text-muted-foreground">
          Current folder: <span className="font-semibold text-foreground">{listing.path}</span>
        </div>
      )}

      <div className="min-h-48 rounded-md border bg-muted/20">
        {isPending && (
          <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
            <Loader2 className="mr-2 size-4 animate-spin" />
            Loading folders...
          </div>
        )}

        {!isPending && listing && (
          <div className="max-h-64 overflow-y-auto">
            <div className="space-y-1 p-2">
              {listing.entries.length === 0 ? (
                <div className="py-8 text-center text-sm text-muted-foreground">
                  No subfolders found
                </div>
              ) : (
                listing.entries.map((entry) => (
                  <button
                    key={entry.path}
                    type="button"
                    onClick={() => {
                      setCurrentPath(entry.path);
                    }}
                    className="flex w-full items-center justify-between rounded-md px-3 py-2 text-sm text-left transition-colors hover:bg-muted"
                  >
                    <div className="flex items-center gap-2 truncate">
                      <Folder className="size-4 shrink-0 text-muted-foreground" />
                      <span className="truncate">{entry.name}</span>
                    </div>
                    {entry.is_git_repo && (
                      <Badge variant="secondary" className="ml-2 shrink-0">
                        git repository
                      </Badge>
                    )}
                  </button>
                ))
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
