import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { components } from "@/api/schema";
import { api, ApiError } from "@/lib/api";
import * as streamModule from "@/lib/stream";
import { TestFakeStream } from "@/pages/inbox/testing";
import { project, stubProjects } from "@/test/projects";
import { renderPage } from "@/test/render";
import { route } from "./route";

type NoteView = components["schemas"]["NoteView"];
type SearchResult = components["schemas"]["SearchResult"];

function note(id: string, overrides: Partial<NoteView> = {}): NoteView {
  return {
    id,
    type: "fact",
    title: `Title ${id}`,
    text: `Text of ${id}`,
    source: null,
    author: "coder",
    created: "2026-10-30T10:00:00Z",
    updated: "2026-10-30T10:00:00Z",
    ...overrides,
  };
}

function result(notes: NoteView[], extra: Partial<SearchResult> = {}): SearchResult {
  return { notes, used_vectors: true, detail: null, ...extra };
}

function ok<T>(data: T) {
  return Promise.resolve({ data, response: new Response(null, { status: 200 }) });
}

const NOTES = "/api/projects/{project_id}/notes";
const NOTE = "/api/projects/{project_id}/notes/{note_id}";

describe("KnowledgePage", () => {
  let fakeStream: TestFakeStream;
  let getSpy: MockInstance | undefined;
  let patchSpy: MockInstance | undefined;
  let deleteSpy: MockInstance | undefined;

  beforeEach(() => {
    fakeStream = new TestFakeStream();
    vi.spyOn(streamModule, "openStream").mockImplementation(() => {
      setTimeout(() => {
        fakeStream.emitOpen();
      }, 0);
      return fakeStream;
    });
    stubProjects([project("prj_1", "Test Project", "2026-10-20T10:00:00Z")]);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubNotes(handler: (query: Record<string, unknown>) => Promise<unknown>) {
    getSpy = vi.spyOn(api, "GET").mockImplementation(((
      path: string,
      init?: { params?: { query?: Record<string, unknown> } },
    ) => {
      if (path === NOTES) return handler(init?.params?.query ?? {});
      if (path === "/api/projects/{project_id}") {
        return ok(project("prj_1", "Test Project", null));
      }
      return Promise.reject(new Error(`Unexpected GET ${path}`));
    }) as never);
  }

  function noteQueries(): Record<string, unknown>[] {
    return (getSpy?.mock.calls ?? [])
      .filter((c) => c[0] === NOTES)
      .map((c) => (c[1] as { params: { query: Record<string, unknown> } }).params.query);
  }

  it("is a project nav entry named Knowledge at order 60", () => {
    expect(route.path).toBe("/projects/:projectId/knowledge");
    expect(route.nav).toMatchObject({ area: "project", label: "Knowledge", order: 60 });
  });

  it("lists notes with their text shown as text", async () => {
    stubNotes(() => ok(result([note("kno_1", { text: "<b>bold</b>" }), note("kno_2")])));
    renderPage("/projects/prj_1/knowledge");

    const row = await screen.findByTestId("note-kno_1");
    expect(within(row).getByText("Title kno_1")).toBeInTheDocument();
    expect(within(row).getByText(/<b>bold<\/b>/)).toBeInTheDocument();
    expect(row.querySelector("b")).toBeNull();
    expect(screen.getByTestId("note-kno_2")).toBeInTheDocument();
  });

  it("searches on submit and filters by type", async () => {
    const user = userEvent.setup();
    stubNotes(() => ok(result([note("kno_1")])));
    renderPage("/projects/prj_1/knowledge");
    await screen.findByTestId("note-kno_1");

    await user.type(screen.getByRole("searchbox", { name: "Search notes" }), "deploy{Enter}");
    await waitFor(() => {
      expect(noteQueries()).toContainEqual(expect.objectContaining({ q: "deploy" }));
    });

    await user.click(screen.getByRole("combobox", { name: "Type" }));
    await user.click(await screen.findByRole("option", { name: "Decision" }));
    await waitFor(() => {
      expect(noteQueries()).toContainEqual(
        expect.objectContaining({ q: "deploy", type: "decision" }),
      );
    });
  });

  it("shows the vector fallback reason only when the server gives one", async () => {
    const user = userEvent.setup();
    stubNotes((query) =>
      ok(
        query.q
          ? result([note("kno_1")], { used_vectors: false, detail: "No embedder is configured" })
          : result([note("kno_1")]),
      ),
    );
    renderPage("/projects/prj_1/knowledge");
    await screen.findByTestId("note-kno_1");
    expect(screen.queryByText(/No embedder is configured/)).toBeNull();

    await user.type(screen.getByRole("searchbox", { name: "Search notes" }), "x{Enter}");
    expect(await screen.findByText(/No embedder is configured/)).toBeInTheDocument();
  });

  it("shows the plain empty state, and the no-match wording when a query is active", async () => {
    const user = userEvent.setup();
    stubNotes((query) => ok(result(query.q ? [] : [note("kno_1")])));
    renderPage("/projects/prj_1/knowledge");
    await screen.findByTestId("note-kno_1");

    await user.type(screen.getByRole("searchbox", { name: "Search notes" }), "zzz{Enter}");
    expect(await screen.findByText("No notes match your search or filter.")).toBeInTheDocument();
    expect(screen.queryByText("No notes yet.")).toBeNull();
  });

  it("shows No notes yet when the project has none", async () => {
    stubNotes(() => ok(result([])));
    renderPage("/projects/prj_1/knowledge");
    expect(await screen.findByText("No notes yet.")).toBeInTheDocument();
  });

  it("shows the server's error with a retry that reloads", async () => {
    const user = userEvent.setup();
    let calls = 0;
    stubNotes(() => {
      calls += 1;
      if (calls === 1)
        return Promise.reject(new ApiError("boom", "The notes are unavailable", 500));
      return ok(result([note("kno_1")]));
    });
    renderPage("/projects/prj_1/knowledge");

    expect(await screen.findByText(/The notes are unavailable/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("note-kno_1")).toBeInTheDocument();
  });

  it("edits and sends only the changed fields, then shows the saved note", async () => {
    const user = userEvent.setup();
    stubNotes(() => ok(result([note("kno_1")])));
    patchSpy = vi
      .spyOn(api, "PATCH")
      .mockImplementation(() => ok(note("kno_1", { title: "Better title" })));
    renderPage("/projects/prj_1/knowledge");
    const row = await screen.findByTestId("note-kno_1");

    await user.click(within(row).getByRole("button", { name: "Edit" }));
    const title = within(row).getByLabelText("Title");
    await user.clear(title);
    await user.type(title, "Better title");
    await user.click(within(row).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith(NOTE, {
        params: { path: { project_id: "prj_1", note_id: "kno_1" } },
        body: { title: "Better title" },
      });
    });
    expect(await within(row).findByText("Better title")).toBeInTheDocument();
    expect(within(row).queryByLabelText("Title")).toBeNull();
  });

  it("changes the type through the editor", async () => {
    const user = userEvent.setup();
    stubNotes(() => ok(result([note("kno_1")])));
    patchSpy = vi
      .spyOn(api, "PATCH")
      .mockImplementation(() => ok(note("kno_1", { type: "runbook" })));
    renderPage("/projects/prj_1/knowledge");
    const row = await screen.findByTestId("note-kno_1");

    await user.click(within(row).getByRole("button", { name: "Edit" }));
    await user.click(within(row).getByRole("combobox", { name: "Note type" }));
    await user.click(await screen.findByRole("option", { name: "Runbook" }));
    await user.click(within(row).getByRole("button", { name: "Save" }));

    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith(
        NOTE,
        expect.objectContaining({ body: { type: "runbook" } }),
      );
    });
  });

  it("does not send a request when nothing changed", async () => {
    const user = userEvent.setup();
    stubNotes(() => ok(result([note("kno_1")])));
    patchSpy = vi.spyOn(api, "PATCH");
    renderPage("/projects/prj_1/knowledge");
    const row = await screen.findByTestId("note-kno_1");

    await user.click(within(row).getByRole("button", { name: "Edit" }));
    expect(within(row).getByRole("button", { name: "Save" })).toBeDisabled();
    expect(patchSpy).not.toHaveBeenCalled();
  });

  it.each([
    ["invalid_note", "Title and text cannot be empty"],
    ["note_too_long", "Title exceeds maximum length of 200 characters"],
  ])("shows the server's %s 422 message as is and keeps the editor open", async (code, message) => {
    const user = userEvent.setup();
    stubNotes(() => ok(result([note("kno_1")])));
    patchSpy = vi
      .spyOn(api, "PATCH")
      .mockImplementation(() => Promise.reject(new ApiError(code, message, 422)));
    renderPage("/projects/prj_1/knowledge");
    const row = await screen.findByTestId("note-kno_1");

    await user.click(within(row).getByRole("button", { name: "Edit" }));
    await user.type(within(row).getByLabelText("Title"), " more");
    await user.click(within(row).getByRole("button", { name: "Save" }));

    expect(await within(row).findByText(message)).toBeInTheDocument();
    expect(within(row).getByLabelText("Title")).toBeInTheDocument();
  });

  it("cancel leaves the note unchanged", async () => {
    const user = userEvent.setup();
    stubNotes(() => ok(result([note("kno_1")])));
    renderPage("/projects/prj_1/knowledge");
    const row = await screen.findByTestId("note-kno_1");

    await user.click(within(row).getByRole("button", { name: "Edit" }));
    await user.type(within(row).getByLabelText("Title"), " more");
    await user.click(within(row).getByRole("button", { name: "Cancel" }));

    expect(within(row).getByText("Title kno_1")).toBeInTheDocument();
  });

  it("asks before deleting, sends the delete and removes the row", async () => {
    const user = userEvent.setup();
    let deleted = false;
    stubNotes(() => ok(result(deleted ? [note("kno_2")] : [note("kno_1"), note("kno_2")])));
    deleteSpy = vi.spyOn(api, "DELETE").mockImplementation((() => {
      deleted = true;
      return Promise.resolve({ data: undefined, response: new Response(null, { status: 204 }) });
    }) as never);
    renderPage("/projects/prj_1/knowledge");
    const row = await screen.findByTestId("note-kno_1");

    await user.click(within(row).getByRole("button", { name: "Delete" }));
    expect(deleteSpy).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Title kno_1/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Delete note" }));

    await waitFor(() => {
      expect(deleteSpy).toHaveBeenCalledWith(NOTE, {
        params: { path: { project_id: "prj_1", note_id: "kno_1" } },
      });
    });
    await waitFor(() => {
      expect(screen.queryByTestId("note-kno_1")).toBeNull();
    });
    expect(screen.getByTestId("note-kno_2")).toBeInTheDocument();
  });

  it("cancelling the confirmation deletes nothing", async () => {
    const user = userEvent.setup();
    stubNotes(() => ok(result([note("kno_1")])));
    deleteSpy = vi.spyOn(api, "DELETE");
    renderPage("/projects/prj_1/knowledge");
    const row = await screen.findByTestId("note-kno_1");

    await user.click(within(row).getByRole("button", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    expect(deleteSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId("note-kno_1")).toBeInTheDocument();
  });

  it("refetches when a note.deleted event arrives", async () => {
    let calls = 0;
    stubNotes(() => {
      calls += 1;
      return ok(result(calls === 1 ? [note("kno_1"), note("kno_2")] : [note("kno_2")]));
    });
    renderPage("/projects/prj_1/knowledge");
    await screen.findByTestId("note-kno_1");

    act(() => {
      fakeStream.emitOpen();
      fakeStream.emit(
        "note.deleted",
        {
          id: "evt_9",
          seq: 9,
          ts: "2026-10-30T10:00:09Z",
          source: "coder",
          type: "note.deleted",
          subject_id: "kno_1",
          payload: {},
        },
        9,
      );
    });

    await waitFor(() => {
      expect(screen.queryByTestId("note-kno_1")).toBeNull();
    });
    expect(screen.getByTestId("note-kno_2")).toBeInTheDocument();
  });
});
