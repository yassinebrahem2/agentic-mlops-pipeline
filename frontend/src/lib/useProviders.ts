import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";

export function useProviders() {
  return useQuery({
    queryKey: ["providers"],
    queryFn: async () => unwrap(await api.GET("/api/providers")),
  });
}
