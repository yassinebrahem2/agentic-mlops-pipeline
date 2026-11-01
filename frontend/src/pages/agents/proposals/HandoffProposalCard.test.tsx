import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import type { components } from "@/api/schema";
import { api } from "@/lib/api";
import { ApprovalCard, type ApprovalPart } from "../ApprovalCard";
import { HandoffProposalCard } from "./HandoffProposalCard";
import type { ProposalView } from "./useProposal";

type HandoffPayload = components["schemas"]["HandoffPayload"];

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });
}

function renderWithClient(ui: ReactNode, queryClient = createTestQueryClient()) {
  return {
    queryClient,
    ...render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>),
  };
}

describe("HandoffProposalCard", () => {
  let getSpy: MockInstance;
  let putSpy: MockInstance;

  const baseApproval: ApprovalPart = {
    approval_id: "apr_handoff_1",
    tool_call_id: "tc_1",
    status: "pending",
    proposal_kind: "handoff",
    preview: {
      title: "Proposal: handoff",
      kind: "text",
      body: "Fallback preview body text for handoff",
    },
    note: null,
    type: "approval",
    kind: "tool",
  };

  const fullPayload: HandoffPayload = {
    models: [
      {
        name: "churn_predictor",
        profile: {
          objective: "Predict customer churn within 30 days",
          problem_type: "binary_classification",
          target_column: "churned",
          positive_class: "yes",
          primary_metric: "f1_score",
          metric_rationale: "Balances precision and recall for churn retention campaigns",
          operating_threshold: 0.65,
          label_delay_days: 14,
          label_maturity_days: 30,
          serving_mode: "batch",
          error_costs: "false_negative: $100, false_positive: $15",
          cost_ratio: 6.67,
          groups: [
            { column: "region", values: ["us-east", "eu-west"] },
            { column: "tier", values: null },
          ],
        },
        predictions: {
          connection: "dw_warehouse",
          table: "predictions_churn",
        },
        labels: {
          connection: "dw_warehouse",
          table: "ground_truth_churn",
        },
        commands: [
          {
            kind: "train",
            type: "command",
            template: "python train_churn.py --epochs {epochs}",
            workflow: null,
            inputs: { epochs: "50" },
            placeholders: ["model", "window_start", "window_end"],
            deploys: false,
            entry_files: ["train_churn.py", "requirements.txt"],
          },
        ],
        promotion: "team_gate",
        environments: {
          prod_alias: "production",
          staging_alias: "staging",
        },
        production_signal: {
          kind: "tracker_alias",
          ref: "prod_alias",
        },
      },
      {
        name: "fraud_detector",
        profile: {
          objective: "Detect transaction fraud in real-time",
          problem_type: "anomaly_detection",
          target_column: "is_fraud",
          positive_class: "1",
          primary_metric: "precision_at_k",
          metric_rationale: "Minimize false blocks while catching high-value fraud",
          operating_threshold: 0.85,
          label_delay_days: 1,
          label_maturity_days: 7,
          serving_mode: "online",
          error_costs: "false_positive: $50 friction, false_negative: transaction value",
          cost_ratio: null,
          groups: [],
        },
        predictions: null,
        labels: null,
        commands: [],
        promotion: "person",
        environments: {},
        production_signal: {
          kind: "ci_job",
          ref: "deploy-fraud-job",
        },
      },
    ],
    commands: [
      {
        kind: "evaluate",
        type: "ci_dispatch",
        template: null,
        workflow: "eval.yml",
        inputs: { test_suite: "full" },
        placeholders: ["candidate_version", "champion_version", "threshold"],
        deploys: false,
        entry_files: ["eval.py"],
      },
      {
        kind: "deploy",
        type: "command",
        template: "deploy.sh --target prod",
        workflow: null,
        inputs: { mode: "canary" },
        placeholders: ["candidate_version", "mode"],
        deploys: true,
        entry_files: ["deploy.sh"],
      },
      {
        kind: "rollback",
        type: "command",
        template: "rollback.sh",
        workflow: null,
        inputs: {},
        placeholders: ["model"],
        deploys: true,
        entry_files: ["rollback.sh"],
      },
      {
        kind: "rerun",
        type: "command",
        template: "rerun.sh",
        workflow: null,
        inputs: {},
        placeholders: ["request_id"],
        deploys: false,
        entry_files: ["rerun.sh"],
      },
      {
        kind: "hold",
        type: "command",
        template: "hold.sh",
        workflow: null,
        inputs: {},
        placeholders: ["model"],
        deploys: false,
        entry_files: ["hold.sh"],
      },
      {
        kind: "fall_back",
        type: "command",
        template: "fallback.sh",
        workflow: null,
        inputs: {},
        placeholders: ["model"],
        deploys: true,
        entry_files: ["fallback.sh"],
      },
    ],
    data_roles: [
      {
        connection: "dw_warehouse",
        table: "events_stream",
        role: "serving_inputs",
        id_column: "event_id",
        time_column: "occurred_at",
      },
      {
        connection: "dw_warehouse",
        table: "training_dataset",
        role: "training",
        id_column: null,
        time_column: null,
      },
    ],
    tracking: [
      {
        type: "mlflow",
        settings: {
          url: "https://mlflow.internal.acme.com",
        },
        auth: "credential",
      },
      {
        type: "github_actions",
        settings: {
          repository: "acme/ml-pipelines",
          workflows: ["train.yml", "ci.yml"],
        },
        auth: "none",
      },
    ],
    pipeline: [
      {
        name: "data_prep",
        entry: "prep.py",
        inputs: ["raw_events", "customer_dim"],
        outputs: ["clean_features"],
      },
      {
        name: "feature_engineering",
        entry: "features.py",
        inputs: ["clean_features"],
        outputs: ["feature_matrix"],
      },
    ],
    edges: [
      {
        from_stage: "data_prep",
        to_stage: "feature_engineering",
      },
    ],
    operations: {
      scheduler: {
        name: "cron_runner",
        config_location: ".github/workflows/schedule.yml",
      },
      feeds: [
        {
          name: "customer_activity_feed",
          kind: "data",
          ref: {
            connection: "dw_warehouse",
            table: "customer_activity",
          },
          cadence: {
            cron: "0 2 * * *",
            every_minutes: null,
          },
          grace_minutes: 30,
          timezone: "UTC",
        },
        {
          name: "upstream_etl_job",
          kind: "job",
          ref: {
            ci_workflow: "etl-nightly.yml",
            file: {
              connection: "s3_archive",
              path_pattern: "etl/*.parquet",
            },
          },
          cadence: {
            cron: null,
            every_minutes: 60,
          },
          grace_minutes: 15,
          timezone: "America/New_York",
        },
      ],
      on_call: [
        {
          name: "Primary ML Engineer",
          contacts: [
            { channel: "discord", address: "@ml-oncall" },
            { channel: "email", address: "ml-oncall@acme.com" },
          ],
          hours: {
            days: ["mon", "tue", "wed", "thu", "fri"],
            start: "09:00",
            end: "17:00",
          },
          timezone: "America/New_York",
          escalation_order: 1,
        },
      ],
      checks: [
        {
          check_id: "chk_data_drift",
          name: "Daily Feature Drift Check",
          category: "drift",
          cadence: {
            cron: "0 4 * * *",
            every_minutes: null,
          },
          timezone: "UTC",
          results: {
            connection: "s3_archive",
            path_pattern: "drift_reports/*.json",
          },
        },
        {
          check_id: "chk_perf_ci",
          name: "Model Performance Regression",
          category: "model_performance",
          cadence: {
            cron: null,
            every_minutes: 120,
          },
          timezone: "UTC",
          results: {
            workflow: "model-check.yml",
          },
        },
        {
          check_id: "chk_mlflow_auc",
          name: "ROC AUC Degradation Check",
          category: "operational",
          cadence: {
            cron: "0 6 * * *",
            every_minutes: null,
          },
          timezone: "UTC",
          results: {
            metric: "val_auc",
            threshold: 0.8,
            fires_when: "below",
          },
        },
      ],
      results_location: {
        connection: "dw_warehouse",
        path_pattern: "quality_reports/*.json",
      },
    },
    gaps: [
      {
        what: "Ground truth labels delayed by 14 days",
        next_step: "Implement proxy metric using customer retention events",
      },
    ],
    grants: {
      network: true,
      permission_level: "ask",
    },
  };

  const sampleProposal: ProposalView = {
    id: "prop_handoff_1",
    key: "handoff/v1",
    kind: "handoff",
    agent: "lead",
    approval_id: "apr_handoff_1",
    status: "pending",
    created: "2026-10-29T10:00:00Z",
    turn_id: "turn_1",
    tool_call_id: "tc_1",
    warnings: ["Notice: external storage configured", "Verify write permissions"],
    slots: [
      {
        index: 0,
        label: "MLflow Tracking Password",
        set: false,
      },
    ],
    result: null,
    payload: fullPayload,
  };

  beforeEach(() => {
    getSpy = vi.spyOn(api, "GET").mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.resolve({
          data: sampleProposal,
          response: new Response(JSON.stringify(sampleProposal), { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    putSpy = vi.spyOn(api, "PUT").mockResolvedValue({
      data: {
        index: 0,
        label: "MLflow Tracking Password",
        set: true,
      },
      response: new Response(null, { status: 200 }),
    });
  });

  afterEach(() => {
    getSpy.mockRestore();
    putSpy.mockRestore();
  });

  it("renders every field of every section in exact order for full payload fixture", async () => {
    renderWithClient(
      <HandoffProposalCard approval={baseApproval} projectId="proj_1" toolName="Handoff" />,
    );

    // Wait for proposal to load
    await waitFor(() => {
      expect(screen.getByText("churn_predictor")).toBeInTheDocument();
    });

    // 1. Models section
    expect(screen.getByRole("heading", { name: "Models" })).toBeInTheDocument();

    // Model 1: churn_predictor
    const churnCard = screen.getByText("churn_predictor").closest("div.rounded-md");
    expect(churnCard).toBeInstanceOf(HTMLElement);
    if (!(churnCard instanceof HTMLElement)) throw new Error("Missing churnCard");

    expect(
      within(churnCard).getByText(/Predict customer churn within 30 days/),
    ).toBeInTheDocument();
    expect(within(churnCard).getByText(/binary_classification/)).toBeInTheDocument();
    expect(within(churnCard).getByText(/churned/)).toBeInTheDocument();
    expect(within(churnCard).getByText(/Positive Class:/).parentElement).toHaveTextContent(
      "Positive Class: yes",
    );
    expect(within(churnCard).getByText(/f1_score/)).toBeInTheDocument();
    expect(
      within(churnCard).getByText("Balances precision and recall for churn retention campaigns"),
    ).toBeInTheDocument();
    expect(within(churnCard).getByText(/0.65/)).toBeInTheDocument();
    expect(within(churnCard).getByText(/Label Delay Days:/).parentElement).toHaveTextContent(
      "Label Delay Days: 14",
    );
    expect(within(churnCard).getByText(/Label Maturity Days:/).parentElement).toHaveTextContent(
      "Label Maturity Days: 30",
    );
    expect(within(churnCard).getByText(/Serving Mode:/).parentElement).toHaveTextContent(
      "Serving Mode: batch",
    );
    expect(
      within(churnCard).getByText("false_negative: $100, false_positive: $15"),
    ).toBeInTheDocument();
    expect(within(churnCard).getByText(/Cost Ratio:/).parentElement).toHaveTextContent(
      "Cost Ratio: 6.67",
    );
    expect(within(churnCard).getByText(/region: us-east, eu-west/)).toBeInTheDocument();
    expect(within(churnCard).getByText("dw_warehouse.predictions_churn")).toBeInTheDocument();
    expect(within(churnCard).getByText("dw_warehouse.ground_truth_churn")).toBeInTheDocument();
    expect(within(churnCard).getByText("team_gate")).toBeInTheDocument();
    expect(within(churnCard).getByText(/prod_alias: production/)).toBeInTheDocument();
    expect(within(churnCard).getByText("tracker_alias (prod_alias)")).toBeInTheDocument();

    // Model 2: fraud_detector
    const fraudCard = screen.getByText("fraud_detector").closest("div.rounded-md");
    expect(fraudCard).toBeInstanceOf(HTMLElement);
    if (!(fraudCard instanceof HTMLElement)) throw new Error("Missing fraudCard");

    expect(
      within(fraudCard).getByText("Detect transaction fraud in real-time"),
    ).toBeInTheDocument();
    expect(within(fraudCard).getByText(/anomaly_detection/)).toBeInTheDocument();
    expect(within(fraudCard).getByText(/is_fraud/)).toBeInTheDocument();
    expect(within(fraudCard).getByText(/Positive Class:/).parentElement).toHaveTextContent(
      "Positive Class: 1",
    );
    expect(within(fraudCard).getByText(/precision_at_k/)).toBeInTheDocument();
    expect(
      within(fraudCard).getByText("Minimize false blocks while catching high-value fraud"),
    ).toBeInTheDocument();
    expect(within(fraudCard).getByText(/0.85/)).toBeInTheDocument();
    expect(within(fraudCard).getByText(/Label Delay Days:/).parentElement).toHaveTextContent(
      "Label Delay Days: 1",
    );
    expect(within(fraudCard).getByText(/Label Maturity Days:/).parentElement).toHaveTextContent(
      "Label Maturity Days: 7",
    );
    expect(within(fraudCard).getByText(/Serving Mode:/).parentElement).toHaveTextContent(
      "Serving Mode: online",
    );
    expect(within(fraudCard).getByText(/Cost Ratio:/).parentElement).toHaveTextContent(
      "Cost Ratio: none",
    );
    expect(within(fraudCard).getByText("person")).toBeInTheDocument();
    expect(within(fraudCard).getByText("ci_job (deploy-fraud-job)")).toBeInTheDocument();

    // 2. Commands section
    expect(screen.getByRole("heading", { name: "Commands" })).toBeInTheDocument();
    expect(screen.getByText("eval.yml")).toBeInTheDocument();
    expect(screen.getByText("deploy.sh --target prod")).toBeInTheDocument();
    expect(screen.getAllByText("rollback.sh")[0]).toBeInTheDocument();
    expect(screen.getAllByText("rerun.sh")[0]).toBeInTheDocument();
    expect(screen.getAllByText("hold.sh")[0]).toBeInTheDocument();
    expect(screen.getAllByText("fallback.sh")[0]).toBeInTheDocument();

    // 3. Data roles section
    expect(screen.getByRole("heading", { name: "Data Roles" })).toBeInTheDocument();
    expect(screen.getByText("events_stream")).toBeInTheDocument();
    expect(screen.getByText("serving_inputs")).toBeInTheDocument();
    expect(screen.getByText("event_id")).toBeInTheDocument();
    expect(screen.getByText("occurred_at")).toBeInTheDocument();
    expect(screen.getByText("training_dataset")).toBeInTheDocument();

    // 4. Tracking section
    expect(screen.getByRole("heading", { name: "Tracking" })).toBeInTheDocument();
    expect(screen.getByText("https://mlflow.internal.acme.com")).toBeInTheDocument();
    expect(screen.getByText(/acme\/ml-pipelines \(train\.yml, ci\.yml\)/)).toBeInTheDocument();

    // 5. Pipeline section
    expect(screen.getByRole("heading", { name: "Pipeline" })).toBeInTheDocument();
    expect(screen.getByText("data_prep")).toBeInTheDocument();
    expect(screen.getByText("(prep.py)")).toBeInTheDocument();
    expect(screen.getByText(/raw_events, customer_dim/)).toBeInTheDocument();
    expect(screen.getAllByText(/clean_features/)[0]).toBeInTheDocument();
    expect(screen.getByText("data_prep to feature_engineering")).toBeInTheDocument();

    // 6. Operations section
    expect(screen.getByRole("heading", { name: "Operations" })).toBeInTheDocument();
    expect(
      screen.getByText(/cron_runner \(\.github\/workflows\/schedule\.yml\)/),
    ).toBeInTheDocument();
    expect(screen.getByText("customer_activity_feed")).toBeInTheDocument();
    expect(screen.getByText(/0 2 \* \* \*/)).toBeInTheDocument();
    expect(screen.getByText(/every 60 minutes/)).toBeInTheDocument();
    expect(screen.getByText("Primary ML Engineer")).toBeInTheDocument();
    expect(screen.getByText(/@ml-oncall/)).toBeInTheDocument();
    expect(screen.getByText(/ml-oncall@acme\.com/)).toBeInTheDocument();
    expect(screen.getByText(/mon, tue, wed, thu, fri \(09:00 - 17:00\)/)).toBeInTheDocument();
    expect(screen.getByText("Daily Feature Drift Check")).toBeInTheDocument();
    expect(screen.getByText(/s3_archive:drift_reports\/\*\.json/)).toBeInTheDocument();
    expect(screen.getByText(/model-check\.yml/)).toBeInTheDocument();
    expect(screen.getByText(/val_auc \(< 0\.8\)/)).toBeInTheDocument();
    expect(screen.getByText(/dw_warehouse:quality_reports\/\*\.json/)).toBeInTheDocument();

    // 7. Gaps section
    expect(screen.getByRole("heading", { name: "Gaps" })).toBeInTheDocument();
    expect(screen.getByText("Ground truth labels delayed by 14 days")).toBeInTheDocument();
    expect(
      screen.getByText("Implement proxy metric using customer retention events"),
    ).toBeInTheDocument();

    // 8. Grants section
    expect(screen.getByRole("heading", { name: "Grants" })).toBeInTheDocument();
    expect(screen.getByText(/Network:/).parentElement).toHaveTextContent("Network: on");
    expect(screen.getByText(/Permission Level:/).parentElement).toHaveTextContent(
      "Permission Level: ask",
    );

    // Warnings rendered above controls
    expect(screen.getByText("Notice: external storage configured")).toBeInTheDocument();
    expect(screen.getByText("Verify write permissions")).toBeInTheDocument();
  });

  it("renders empty sections as 'none' for minimal payload", async () => {
    const minimalProposal: ProposalView = {
      ...sampleProposal,
      warnings: [],
      slots: [],
      payload: {
        models: [],
        commands: [],
        data_roles: [],
        tracking: [],
        pipeline: [],
        edges: [],
        operations: {
          scheduler: null,
          feeds: [],
          on_call: [],
          checks: [],
          results_location: {
            connection: "local_store",
            path_pattern: "out/*.json",
          },
        },
        gaps: [],
        grants: {
          network: false,
          permission_level: "suggest",
        },
      },
    };

    getSpy.mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.resolve({
          data: minimalProposal,
          response: new Response(JSON.stringify(minimalProposal), { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    renderWithClient(
      <HandoffProposalCard approval={baseApproval} projectId="proj_1" toolName="Handoff" />,
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Models" })).toBeInTheDocument();
    });

    // Check that empty sections display "none"
    const noneElements = screen.getAllByText("none");
    expect(noneElements.length).toBeGreaterThanOrEqual(5);

    // No warnings container rendered when empty
    expect(screen.queryByText("Notice: external storage configured")).not.toBeInTheDocument();
  });

  it("posts credential once and clears input state, never leaking value to DOM or query cache", async () => {
    const user = userEvent.setup();
    const queryClient = createTestQueryClient();

    renderWithClient(
      <HandoffProposalCard approval={baseApproval} projectId="proj_1" toolName="Handoff" />,
      queryClient,
    );

    await waitFor(() => {
      expect(screen.getByLabelText("MLflow Tracking Password")).toBeInTheDocument();
    });

    // Slot unset initial status
    expect(screen.getByText(/credential set: no/i)).toBeInTheDocument();
    expect(
      screen.getByText(
        /the source is created without a credential; add one under Tracking sources in the project settings/i,
      ),
    ).toBeInTheDocument();

    const input = screen.getByLabelText<HTMLInputElement>("MLflow Tracking Password");
    const saveButton = screen.getByRole("button", { name: "Save" });

    // Disabled when empty
    expect(saveButton).toBeDisabled();

    // Type secret
    const secretValue = "super-secret-password-1234";
    await user.type(input, secretValue);
    expect(saveButton).not.toBeDisabled();

    // Click Save
    await user.click(saveButton);

    // Assert PUT was called with secret
    expect(putSpy).toHaveBeenCalledTimes(1);
    expect(putSpy).toHaveBeenCalledWith(
      "/api/projects/{project_id}/proposals/{proposal_id}/credentials/{slot}",
      {
        params: {
          path: {
            project_id: "proj_1",
            proposal_id: "prop_handoff_1",
            slot: 0,
          },
        },
        body: { value: secretValue },
      },
    );

    // Immediately cleared from input
    expect(input.value).toBe("");

    // Assert secret is nowhere in DOM
    expect(screen.queryByText(secretValue)).not.toBeInTheDocument();
    expect(document.body.innerHTML).not.toContain(secretValue);

    // Assert secret is nowhere in query cache
    const queries = queryClient.getQueryCache().getAll();
    for (const q of queries) {
      expect(JSON.stringify(q.state.data)).not.toContain(secretValue);
    }

    // Now shows "credential set: yes"
    await waitFor(() => {
      expect(screen.getByText(/credential set: yes/i)).toBeInTheDocument();
    });
  });

  it("clears input on failed save and displays error alert", async () => {
    const user = userEvent.setup();
    putSpy.mockResolvedValue({
      error: { code: "keystore_error", message: "KeyStore is locked" },
      response: new Response(
        JSON.stringify({ code: "keystore_error", message: "KeyStore is locked" }),
        { status: 500 },
      ),
    });

    renderWithClient(
      <HandoffProposalCard approval={baseApproval} projectId="proj_1" toolName="Handoff" />,
    );

    await waitFor(() => {
      expect(screen.getByLabelText("MLflow Tracking Password")).toBeInTheDocument();
    });

    const input = screen.getByLabelText<HTMLInputElement>("MLflow Tracking Password");
    const saveButton = screen.getByRole("button", { name: "Save" });

    const failedSecret = "bad-secret-token";
    await user.type(input, failedSecret);
    await user.click(saveButton);

    // Input must be cleared even on error!
    expect(input.value).toBe("");
    expect(document.body.innerHTML).not.toContain(failedSecret);

    // Error alert displayed
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("KeyStore is locked");
    });
  });

  it("disables Save when proposal is not pending", async () => {
    const decidedApproval: ApprovalPart = {
      ...baseApproval,
      status: "approved",
    };
    const decidedProposal: ProposalView = {
      ...sampleProposal,
      status: "approved",
    };

    getSpy.mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.resolve({
          data: decidedProposal,
          response: new Response(JSON.stringify(decidedProposal), { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    renderWithClient(
      <HandoffProposalCard approval={decidedApproval} projectId="proj_1" toolName="Handoff" />,
    );

    await waitFor(() => {
      expect(screen.getByLabelText("MLflow Tracking Password")).toBeInTheDocument();
    });

    const saveButton = screen.getByRole("button", { name: "Save" });
    expect(saveButton).toBeDisabled();
  });

  it("renders applied and failed result on decided card", async () => {
    const decidedApproval: ApprovalPart = {
      ...baseApproval,
      status: "approved",
    };
    const decidedProposal: ProposalView = {
      ...sampleProposal,
      status: "approved",
      result: {
        applied: ["handoff version 1 stored", "scheduler configured"],
        failed: {
          "tracking credentials": "Vault access denied",
        },
      },
    };

    getSpy.mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.resolve({
          data: decidedProposal,
          response: new Response(JSON.stringify(decidedProposal), { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    renderWithClient(
      <HandoffProposalCard approval={decidedApproval} projectId="proj_1" toolName="Handoff" />,
    );

    await waitFor(() => {
      expect(
        screen.getByText("handoff version 1 stored, scheduler configured"),
      ).toBeInTheDocument();
    });

    expect(screen.getByText("tracking credentials")).toBeInTheDocument();
    expect(screen.getByText(/Vault access denied/)).toBeInTheDocument();
  });

  it("is dispatched by ApprovalCard for proposal_kind 'handoff'", async () => {
    renderWithClient(
      <ApprovalCard approval={baseApproval} projectId="proj_1" toolName="Handoff" />,
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Models" })).toBeInTheDocument();
    });
  });

  it("renders 'not judged for silence' for check without a cadence", async () => {
    const noCadenceProposal: ProposalView = {
      ...sampleProposal,
      warnings: [],
      slots: [],
      payload: {
        ...fullPayload,
        operations: {
          ...fullPayload.operations,
          checks: [
            {
              check_id: "chk_on_demand",
              name: "On Demand Evaluation",
              category: "model_performance",
              cadence: null,
              timezone: "UTC",
              results: {
                metric: "accuracy",
                threshold: 0.9,
                fires_when: "below",
              },
            },
          ],
        },
      },
    };

    getSpy.mockImplementation((url) => {
      if (url === "/api/projects/{project_id}/proposals/by-approval/{approval_id}") {
        return Promise.resolve({
          data: noCadenceProposal,
          response: new Response(JSON.stringify(noCadenceProposal), { status: 200 }),
        });
      }
      return Promise.resolve({ data: {}, response: new Response(null, { status: 200 }) });
    });

    renderWithClient(
      <HandoffProposalCard approval={baseApproval} projectId="proj_1" toolName="Handoff" />,
    );

    await waitFor(() => {
      expect(screen.getByText("On Demand Evaluation")).toBeInTheDocument();
    });

    expect(screen.getByText(/not judged for silence/)).toBeInTheDocument();
  });
});
