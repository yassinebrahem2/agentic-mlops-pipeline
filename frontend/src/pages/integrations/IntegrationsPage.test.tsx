import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../../api/schema";
import { renderPage } from "../../test/render";

type GitHubStatus = components["schemas"]["GitHubStatus"];
type Sent = { method: string; url: URL; body: unknown };
type Handler = (sent: Sent) => Response | Promise<Response>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubApi(handlers: Record<string, Handler>): Sent[] {
  const sent: Sent[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: Request) => {
      const text = await input.text();
      const request: Sent = {
        method: input.method,
        url: new URL(input.url),
        body: text ? (JSON.parse(text) as unknown) : undefined,
      };
      sent.push(request);
      const handler = handlers[`${request.method} ${request.url.pathname}`];
      if (!handler) throw new Error(`Unexpected ${request.method} ${request.url.pathname}`);
      return handler(request);
    }),
  );
  return sent;
}

function writes(sent: Sent[]): Sent[] {
  return sent.filter((s) => s.method !== "GET");
}

const notConnected: GitHubStatus = { mode: "none", login: null, gh_available: false };
const notConnectedWithGh: GitHubStatus = { mode: "none", login: null, gh_available: true };
const throughGh: GitHubStatus = { mode: "gh", login: "octocat", gh_available: true };
const withToken: GitHubStatus = { mode: "token", login: "octocat", gh_available: false };

const fakeToken = "github_pat_test_not_real";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Integrations page", () => {
  it("offers a token, and no GitHub CLI login, when gh is not installed", async () => {
    stubApi({ "GET /api/github": () => json(notConnected) });
    renderPage("/integrations");

    const input = await screen.findByLabelText("GitHub token");
    expect(input).toHaveAttribute("type", "password");
    expect(screen.getByText(/Contents: read/)).toBeInTheDocument();
    expect(screen.getByText(/Metadata: read/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect" })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Use my GitHub CLI login" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disconnect" })).not.toBeInTheDocument();
  });

  it("offers the GitHub CLI login when gh is installed", async () => {
    stubApi({ "GET /api/github": () => json(notConnectedWithGh) });
    renderPage("/integrations");

    expect(
      await screen.findByRole("button", { name: "Use my GitHub CLI login" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("GitHub token")).toBeInTheDocument();
  });

  it("shows a connection made through the GitHub CLI", async () => {
    stubApi({ "GET /api/github": () => json(throughGh) });
    renderPage("/integrations");

    expect(await screen.findByText("Connected as octocat")).toBeInTheDocument();
    expect(screen.getByText("through the GitHub CLI")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Disconnect" })).toBeInTheDocument();
    expect(screen.queryByLabelText("GitHub token")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Use my GitHub CLI login" }),
    ).not.toBeInTheDocument();
  });

  it("shows a connection made with a pasted token", async () => {
    stubApi({ "GET /api/github": () => json(withToken) });
    renderPage("/integrations");

    expect(await screen.findByText("Connected as octocat")).toBeInTheDocument();
    expect(screen.getByText("with a pasted token")).toBeInTheDocument();
    expect(screen.queryByLabelText("GitHub token")).not.toBeInTheDocument();
  });

  it("connects through the GitHub CLI login", async () => {
    const sent = stubApi({
      "GET /api/github": () => json(notConnectedWithGh),
      "POST /api/github/gh": () => json(throughGh),
    });
    const user = userEvent.setup();
    renderPage("/integrations");

    await user.click(await screen.findByRole("button", { name: "Use my GitHub CLI login" }));

    expect(await screen.findByText("Connected as octocat")).toBeInTheDocument();
    expect(screen.getByText("through the GitHub CLI")).toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      ["POST", "/api/github/gh", undefined],
    ]);
  });

  it("shows the server's message when the GitHub CLI is not logged in", async () => {
    const message = "The GitHub CLI is not logged in. Run `gh auth login`, then try again.";
    stubApi({
      "GET /api/github": () => json(notConnectedWithGh),
      "POST /api/github/gh": () => json({ code: "gh_not_logged_in", message, detail: null }, 409),
    });
    const user = userEvent.setup();
    renderPage("/integrations");

    await user.click(await screen.findByRole("button", { name: "Use my GitHub CLI login" }));

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(screen.queryByText(/Connected as/)).not.toBeInTheDocument();
  });

  it("sends a pasted token once, clears the input and never shows the token again", async () => {
    const sent = stubApi({
      "GET /api/github": () => json(notConnected),
      "POST /api/github/token": () => json(withToken),
    });
    const user = userEvent.setup();
    renderPage("/integrations");

    await user.type(await screen.findByLabelText("GitHub token"), fakeToken);
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByText("Connected as octocat")).toBeInTheDocument();
    expect(screen.getByText("with a pasted token")).toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname, s.body])).toEqual([
      ["POST", "/api/github/token", { token: fakeToken }],
    ]);
    expect(screen.queryByDisplayValue(fakeToken)).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(fakeToken);
  });

  it("shows the server's message when the token is rejected, and clears the input", async () => {
    const message =
      "GitHub rejected the token. Log in again with `gh auth login` or paste a new token in Integrations.";
    stubApi({
      "GET /api/github": () => json(notConnected),
      "POST /api/github/token": () =>
        json({ code: "github_token_rejected", message, detail: null }, 422),
    });
    const user = userEvent.setup();
    renderPage("/integrations");

    const input = await screen.findByLabelText("GitHub token");
    await user.type(input, fakeToken);
    await user.click(screen.getByRole("button", { name: "Connect" }));

    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(input).toHaveValue("");
    expect(document.body.innerHTML).not.toContain(fakeToken);
  });

  it("asks before disconnecting, then sends DELETE", async () => {
    let connected = true;
    const sent = stubApi({
      "GET /api/github": () => json(connected ? throughGh : notConnectedWithGh),
      "DELETE /api/github": () => {
        connected = false;
        return new Response(null, { status: 204 });
      },
    });
    const user = userEvent.setup();
    renderPage("/integrations");

    await user.click(await screen.findByRole("button", { name: "Disconnect" }));
    const dialog = await screen.findByRole("dialog");
    expect(writes(sent)).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Disconnect" }));

    expect(
      await screen.findByRole("button", { name: "Use my GitHub CLI login" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.queryByText(/Connected as/)).not.toBeInTheDocument();
    expect(writes(sent).map((s) => [s.method, s.url.pathname])).toEqual([
      ["DELETE", "/api/github"],
    ]);
  });

  it("does nothing when disconnecting is cancelled", async () => {
    const sent = stubApi({ "GET /api/github": () => json(throughGh) });
    const user = userEvent.setup();
    renderPage("/integrations");

    await user.click(await screen.findByRole("button", { name: "Disconnect" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Connected as octocat")).toBeInTheDocument();
    expect(writes(sent)).toEqual([]);
  });

  it("says so when the GitHub status cannot be read", async () => {
    stubApi({
      "GET /api/github": () =>
        json(
          { code: "keystore_unavailable", message: "The key store is locked.", detail: null },
          503,
        ),
    });
    renderPage("/integrations");

    expect(await screen.findByText("The key store is locked.")).toBeInTheDocument();
  });
});
