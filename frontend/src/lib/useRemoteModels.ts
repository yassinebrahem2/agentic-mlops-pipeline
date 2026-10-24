import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";

export function useRemoteModels(provider: string, options?: { enabled?: boolean }) {
  const enabled = options?.enabled ?? true;
  return useQuery({
    queryKey: ["remote-models", provider],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/providers/{name}/remote-models", {
          params: { path: { name: provider } },
        }),
      ),
    enabled: enabled && provider !== "",
  });
}
