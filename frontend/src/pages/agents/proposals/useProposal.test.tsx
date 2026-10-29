import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "@/lib/api";
import { proposalByApprovalKey, useProposal, type ProposalView } from "./useProposal";

function createWrapper() {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  };
}

describe("useProposal", () => {
  let getSpy: MockInstance;

  beforeEach(() => {
    getSpy = vi.spyOn(api, "GET");
  });

  afterEach(() => {
    getSpy.mockRestore();
  });

  it("builds the correct query key", () => {
    expect(proposalByApprovalKey("prj_1", "apr_1")).toEqual([
      "projects",
      "prj_1",
      "proposals",
      "by-approval",
      "apr_1",
    ]);
  });

  it("fetches proposal by approval id", async () => {
    const mockProposal: ProposalView = {
      id: "prop_1",
      key: "connections/local_folder",
      kind: "connections",
      agent: "lead",
      approval_id: "apr_1",
      status: "pending",
      created: "2026-10-29T10:00:00Z",
      turn_id: "turn_1",
      tool_call_id: "tc_1",
      warnings: [],
      slots: [],
      result: null,
      payload: {
        connections: [],
      },
    };

    getSpy.mockResolvedValueOnce({
      data: mockProposal,
      response: new Response(JSON.stringify(mockProposal), { status: 200 }),
    });

    const { result } = renderHook(() => useProposal("prj_1", "apr_1"), {
      wrapper: createWrapper(),
    });

    await waitFor(() => {
      expect(result.current.isSuccess).toBe(true);
    });

    expect(result.current.data).toEqual(mockProposal);
    expect(getSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/proposals/by-approval/{approval_id}",
      {
        params: {
          path: {
            project_id: "prj_1",
            approval_id: "apr_1",
          },
        },
      },
    );
  });

  it("is disabled when projectId or approvalId is missing", () => {
    const { result } = renderHook(() => useProposal(undefined, undefined), {
      wrapper: createWrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(getSpy).not.toHaveBeenCalled();
  });
});
