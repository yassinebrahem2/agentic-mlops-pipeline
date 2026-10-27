import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { EventRecord } from "@/lib/useProjectEvents";
import { api } from "@/lib/api";
import type { TaskView } from "../TaskBar";
import { FakeStream, initialSession, setupSessionStreamMock, wrapper } from "./testing";
import { useSession } from "./useSession";

const task: TaskView = {
  id: "tsk_1",
  agent: "orchestrator",
  title: "Write notes",
  branch: "task/write-notes",
  status: "active",
  review_kind: "local",
  pr_number: null,
  pr_url: null,
};

function taskEvent(type: "task.started" | "task.ended", source: string, seq: number): EventRecord {
  return {
    id: `evt_${String(seq)}`,
    type,
    source,
    subject_id: task.id,
    payload: { task_id: task.id },
    seq,
    ts: "2026-10-27T00:00:00Z",
  };
}

describe("useSession current task", () => {
  let createdStreams: FakeStream[] = [];
  let eventStreams: FakeStream[] = [];
  let openStreamSpy: MockInstance | undefined;
  let getSpy: MockInstance | undefined;
  let snapshotTask: TaskView | null = null;

  beforeEach(() => {
    createdStreams = [];
    eventStreams = [];
    snapshotTask = null;
    openStreamSpy = setupSessionStreamMock(createdStreams, eventStreams);
    getSpy = vi.spyOn(api, "GET").mockImplementation(() =>
      Promise.resolve({
        data: { ...initialSession, current_task: snapshotTask },
        response: new Response(null, { status: 200 }),
      }),
    );
  });

  afterEach(() => {
    openStreamSpy?.mockRestore();
    vi.restoreAllMocks();
  });

  async function renderSession() {
    const hook = renderHook(() => useSession("prj_1", "orchestrator"), { wrapper });
    await waitFor(() => {
      expect(createdStreams).toHaveLength(1);
    });
    expect(eventStreams).toHaveLength(1);
    expect(eventStreams[0]?.url).toBe("/api/projects/prj_1/events/stream");
    return hook;
  }

  function emitTaskEvent(event: EventRecord) {
    act(() => {
      eventStreams[0]?.emit(event.type, event, event.seq);
    });
  }

  it("shows a task as soon as task.started arrives, before the turn ends", async () => {
    const { result, unmount } = await renderSession();
    expect(result.current.state?.current_task).toBeNull();

    snapshotTask = task;
    emitTaskEvent(taskEvent("task.started", "orchestrator", 1));

    await waitFor(() => {
      expect(result.current.state?.current_task?.id).toBe("tsk_1");
    });
    unmount();
  });

  it("drops the task when task.ended arrives and the snapshot no longer has it", async () => {
    snapshotTask = task;
    const { result, unmount } = await renderSession();
    expect(result.current.state?.current_task?.id).toBe("tsk_1");

    snapshotTask = null;
    emitTaskEvent(taskEvent("task.ended", "orchestrator", 2));

    await waitFor(() => {
      expect(result.current.state?.current_task).toBeNull();
    });
    unmount();
  });

  it("refreshes the task when the project stream resyncs", async () => {
    const { result, unmount } = await renderSession();

    snapshotTask = task;
    act(() => {
      eventStreams[0]?.emit("resync", {}, 4);
    });

    await waitFor(() => {
      expect(result.current.state?.current_task?.id).toBe("tsk_1");
    });
    unmount();
  });

  it("ignores task events of another agent", async () => {
    const { result, unmount } = await renderSession();
    const callsBefore = getSpy?.mock.calls.length;

    snapshotTask = task;
    emitTaskEvent(taskEvent("task.started", "analyst", 3));

    await Promise.resolve();
    expect(getSpy?.mock.calls.length).toBe(callsBefore);
    expect(result.current.state?.current_task).toBeNull();
    unmount();
  });
});
