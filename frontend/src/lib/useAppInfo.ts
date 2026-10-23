import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";

export function useAppInfo() {
  return useQuery({
    queryKey: ["app"],
    queryFn: async () => unwrap(await api.GET("/api/app")),
  });
}
