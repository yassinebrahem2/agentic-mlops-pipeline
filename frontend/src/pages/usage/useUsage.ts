import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";

export function useUsage() {
  return useQuery({
    queryKey: ["usage"],
    queryFn: async () => unwrap(await api.GET("/api/usage")),
  });
}
