import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { AgentStatus } from "@/lib/useAgentStatus";
import { AgentList } from "./AgentList";

describe("AgentList", () => {
  const mockAgents: AgentStatus[] = [
    {
      agent: "orchestrator",
      display_name: "Orchestrator",
      state: "idle",
      task: null,
      open_prs: [],
      last_finished: null,
    },
    {
      agent: "coder",
      display_name: "Coder",
      state: "running",
      task: {
        id: "tsk_1",
        title: "Implement feature",
        branch: "feat-1",
        status: "active",
        review_kind: null,
        pr_number: null,
        pr_url: null,
      },
      open_prs: [],
      last_finished: null,
    },
    {
      agent: "reviewer",
      display_name: "Reviewer",
      state: "waiting_approval",
      task: null,
      open_prs: [],
      last_finished: null,
    },
    {
      agent: "tester",
      display_name: "Tester",
      state: "waiting_reply",
      task: null,
      open_prs: [],
      last_finished: null,
    },
    {
      agent: "alert_prioritization",
      display_name: "Alert Prioritization",
      state: "inactive",
      task: null,
      open_prs: [],
      last_finished: "starts in a later stage",
    },
  ];

  it("renders every agent with its state words and dots", () => {
    render(<AgentList agents={mockAgents} selectedAgent="orchestrator" onSelectAgent={vi.fn()} />);

    expect(screen.getByText("Orchestrator")).toBeInTheDocument();
    expect(screen.getByText("idle")).toBeInTheDocument();

    expect(screen.getByText("Coder")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();

    expect(screen.getByText("Reviewer")).toBeInTheDocument();
    expect(screen.getByText("waiting for approval")).toBeInTheDocument();

    expect(screen.getByText("Tester")).toBeInTheDocument();
    expect(screen.getByText("waiting for a reply")).toBeInTheDocument();

    expect(screen.getByText("Alert Prioritization")).toBeInTheDocument();
    expect(screen.getByText("inactive")).toBeInTheDocument();
    expect(screen.getByText("(starts in a later stage)")).toBeInTheDocument();
  });

  it("renders a badge for agents waiting for approval", () => {
    render(<AgentList agents={mockAgents} selectedAgent="orchestrator" onSelectAgent={vi.fn()} />);

    const badge = screen.getByText("Approval");
    expect(badge).toBeInTheDocument();
    expect(badge.closest("[data-slot='badge']")).toBeInTheDocument();
  });

  it("calls onSelectAgent when clicking an active agent", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<AgentList agents={mockAgents} selectedAgent="orchestrator" onSelectAgent={onSelect} />);

    const coderBtn = screen.getByRole("button", { name: /Coder/ });
    await user.click(coderBtn);

    expect(onSelect).toHaveBeenCalledWith("coder");
  });

  it("disables inactive agents and prevents selection", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<AgentList agents={mockAgents} selectedAgent="orchestrator" onSelectAgent={onSelect} />);

    const inactiveBtn = screen.getByRole("button", { name: /Alert Prioritization/ });
    expect(inactiveBtn).toBeDisabled();
    expect(inactiveBtn).toHaveAttribute("aria-disabled", "true");

    await user.click(inactiveBtn);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("marks the selected agent with aria-current", () => {
    render(<AgentList agents={mockAgents} selectedAgent="coder" onSelectAgent={vi.fn()} />);

    const coderBtn = screen.getByRole("button", { name: /Coder/ });
    expect(coderBtn).toHaveAttribute("aria-current", "true");

    const orchBtn = screen.getByRole("button", { name: /Orchestrator/ });
    expect(orchBtn).not.toHaveAttribute("aria-current");
  });
});
