import { vi } from "vitest";
import type { components } from "@/api/schema";

type Project = components["schemas"]["Project"];

export function project(id: string, name: string, lastOpenedAt: string | null): Project {
  return {
    id,
    name,
    source: "local",
    status: "ok",
    status_detail: null,
    created_at: "2026-10-01T10:00:00Z",
    last_opened_at: lastOpenedAt,
    local_path: `/w/${id}`,
    github_full_name: null,
    default_branch: "main",
  };
}

export function stubProjects(projects: Project[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify(projects), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
}
