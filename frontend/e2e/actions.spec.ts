import { randomBytes } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";
import { APP_NAME } from "../src/config";
import { ACCESS_TOKEN, APP_URL, FAKE_BASE_URL, FIXTURE_DIR, SEEDED_MLFLOW_URL } from "./env";
import { createTestRepo, type TestRepo } from "./repo";

// The scripted models' strings (backend/tests/fakes/monitoring_script.py: PROMOTE_REQUEST,
// ONBOARDING_COMPLETE) and the seeded fake MLflow's model and versions
// (backend/tests/e2e/seeded_mlflow.py: the champion is version 1, the challenger version 2).
const PROMOTE_REQUEST = "Propose the promotion of the newest candidate.";
const ONBOARDING_COMPLETE = "Onboarding is complete.";
const MODEL_NAME = "hotel-cancel";
const CHAMPION_VERSION = "1";
const CHALLENGER_VERSION = "2";
const CHAMPION_F1 = "0.52";
const CHALLENGER_F1 = "0.71";
const REQUIRED_ASSESSMENTS = [
  "fix_chosen",
  "data_window",
  "leakage",
  "label_maturity",
  "imbalance",
  "training_serving_skew",
  "risks_and_watch",
  "rollback_plan",
];

const TABLE_FILES: Record<string, string> = {
  bookings: [
    "booking_id,arrival_date,country,lead_time,canceled",
    "B001,2026-10-31,PRT,10,0",
    "B002,2026-10-31,GBR,20,1",
    "B003,2026-10-31,FRA,30,0",
  ].join("\n"),
  predictions: [
    "booking_id,scored_at,score",
    "B001,2026-10-31T06:00:00,0.1",
    "B002,2026-10-31T06:00:00,0.8",
    "B003,2026-10-31T06:00:00,0.2",
  ].join("\n"),
  labels: ["booking_id,canceled", "B001,0", "B002,1", "B003,0"].join("\n"),
};

/** The three tables the scripted handoff names, in a folder of the run's own. */
function createDataFolder(): string {
  const path = mkdtempSync(join(FIXTURE_DIR, "promotion-"));
  for (const [table, content] of Object.entries(TABLE_FILES)) {
    writeFileSync(join(path, `${table}.csv`), `${content}\n`);
  }
  return path;
}

// The handoff's hooks run files of the team's repository; a hook whose entry file the approved
// commit lacks is suspended by the pin check, and a suspended deploy hook refuses a promotion.
const HOOK_ENTRY_FILES = ["rollback.py", "deploy.py"];

/** Commits the hooks' entry files to the test repository's default branch. */
function commitHookEntryFiles(repo: TestRepo): void {
  const git = (...args: string[]) => {
    execFileSync("git", args, {
      cwd: repo.path,
      env: {
        ...process.env,
        GIT_AUTHOR_NAME: "E2E Test",
        GIT_AUTHOR_EMAIL: "e2e@example.com",
        GIT_COMMITTER_NAME: "E2E Test",
        GIT_COMMITTER_EMAIL: "e2e@example.com",
      },
      stdio: "pipe",
    });
  };
  for (const file of HOOK_ENTRY_FILES) {
    writeFileSync(join(repo.path, file), `print("${file}")\n`);
  }
  git("add", ...HOOK_ENTRY_FILES);
  git("-c", "commit.gpgsign=false", "commit", "-m", "Add the hooks' entry files");
}

test("stage 5 actions: a promotion proposal shows its evidence, Reject records it, Deployments shows the version", async ({
  page,
}) => {
  // Onboarding and a tracking sync of the seeded MLflow do not fit the default.
  test.setTimeout(240_000);
  const repo = createTestRepo();
  try {
    commitHookEntryFiles(repo);
    const dataPath = createDataFolder();
    const providerName = `fake-${randomBytes(3).toString("hex")}`;
    const address = (model: string) => `${providerName}:${model}`;
    const messages = page.getByRole("log", { name: "Messages" });
    let projectId = "";

    await test.step("sign in; the scripted models are added, verified and assigned", async () => {
      await page.goto(`${APP_URL}/#token=${ACCESS_TOKEN}`);
      await expect(page.getByRole("heading", { name: APP_NAME, exact: true })).toBeVisible();

      await page.getByRole("link", { name: "AI providers" }).click();
      await expect(page.getByRole("heading", { name: "AI providers", level: 1 })).toBeVisible();
      await page.getByRole("button", { name: "Add provider" }).click();
      const providerDialog = page.getByRole("dialog", { name: "Add a provider" });
      await providerDialog.getByLabel("Name").fill(providerName);
      await providerDialog.getByRole("radio", { name: "Self-hosted" }).check();
      await providerDialog.getByLabel("Base URL").fill(FAKE_BASE_URL);
      await providerDialog.getByRole("button", { name: "Add provider" }).click();
      await expect(providerDialog).toBeHidden();

      for (const model of ["fake-onboarding-promotion", "fake-retraining"]) {
        await page.getByRole("button", { name: "Add model" }).click();
        const modelDialog = page.getByRole("dialog", { name: "Add a model" });
        await modelDialog.getByRole("combobox", { name: "Provider" }).click();
        await page.getByRole("option", { name: providerName }).click();
        await modelDialog.getByRole("combobox", { name: "Model" }).click();
        await page.getByRole("option", { name: model }).click();
        await expect(modelDialog.getByRole("combobox", { name: "Model" })).toHaveValue(model);
        await modelDialog.getByRole("button", { name: "Add model" }).click();
        await expect(modelDialog).toBeHidden();

        await page.getByRole("button", { name: `Verify ${address(model)}`, exact: true }).click();
        const row = page.getByRole("row").filter({ hasText: address(model) });
        await expect(row.getByText("Verified")).toBeVisible();
      }

      const assign = async (agent: string, model: string) => {
        const combobox = page.getByRole("combobox", { name: `Model for ${agent}` });
        await combobox.click();
        await page.getByRole("option", { name: address(model), exact: true }).click();
        await expect(combobox).toHaveText(address(model));
      };
      await assign("Onboarding & Diagnosis", "fake-onboarding-promotion");
      await assign("Retraining", "fake-retraining");
    });

    await test.step("onboard against the seeded MLflow: the handoff declares a deploy hook", async () => {
      await page.getByRole("link", { name: "Projects", exact: true }).click();
      await page.getByRole("link", { name: "Add project" }).first().click();
      await page.getByRole("tab", { name: "Local folder" }).click();
      await expect(page.getByText("Current folder:")).toBeVisible();
      await page.getByLabel("Folder path").fill(repo.path);
      await page.getByRole("button", { name: "Go", exact: true }).click();
      await expect(page.getByText(`Current folder: ${repo.path}`)).toBeVisible();
      await page.getByRole("button", { name: "Use this folder" }).click();

      await expect(page).toHaveURL(/\/projects\/[^/]+\/onboarding/);
      projectId = /\/projects\/([^/]+)\//.exec(page.url())?.[1] ?? "";
      expect(projectId).not.toBe("");
      await expect(page.getByText(`Ready: ${address("fake-onboarding-promotion")}`)).toBeVisible();
      await page.getByRole("button", { name: "Start", exact: true }).click();

      await expect(page).toHaveURL(/\/agents\?agent=onboarding/);
      await expect(messages.getByText(/DATA_PATH=/)).toBeVisible();
      await page
        .getByLabel("Message Onboarding & Diagnosis")
        .fill(`DATA_PATH=${dataPath}\nMLFLOW_URL=${SEEDED_MLFLOW_URL}`);
      await page.getByRole("button", { name: "Send", exact: true }).click();

      const card = (kind: string) =>
        messages
          .locator('[data-testid^="approval-card-"]')
          .filter({ hasText: `Proposal: ${kind}` });
      await card("connections").getByRole("button", { name: "Approve" }).click();
      await card("handoff").getByRole("button", { name: "Approve" }).click();
      await expect(messages.getByText(ONBOARDING_COMPLETE, { exact: true })).toBeVisible();
      await expect(messages.getByRole("status", { name: "Typing..." })).toHaveCount(0);
    });

    await test.step("the tracking sync stores the champion and the challenger", async () => {
      const versions = `${APP_URL}/api/projects/${projectId}/tracking/versions?model=${MODEL_NAME}`;
      await expect
        .poll(
          async () => {
            const response = await page.request.get(versions);
            const body = (await response.json()) as { total?: number };
            return body.total ?? 0;
          },
          { timeout: 120_000, intervals: [2_000] },
        )
        .toBeGreaterThanOrEqual(2);
    });

    const proposal = messages
      .locator('[data-testid^="approval-card-"]')
      .filter({ hasText: "Proposal: promotion" });

    await test.step("a promotion proposal shows its assessments, the computed values and champion against challenger", async () => {
      await page.goto(`${APP_URL}/projects/${projectId}/agents?agent=retraining`);
      await page.getByLabel("Message Retraining").fill(PROMOTE_REQUEST);
      await page.getByRole("button", { name: "Send", exact: true }).click();

      await expect(proposal).toBeVisible({ timeout: 60_000 });
      await expect(proposal).toContainText(`Model: ${MODEL_NAME}`);
      await expect(proposal).toContainText(`Candidate Version: ${CHALLENGER_VERSION}`);
      await expect(proposal).toContainText(`Champion Version: ${CHAMPION_VERSION}`);

      // The evaluation is the team's record, marked as computed by the team's step.
      await expect(proposal.getByText("computed by team step")).toBeVisible();
      const metrics = proposal.getByRole("row").filter({ hasText: "f1" }).first();
      await expect(metrics).toContainText(CHAMPION_F1);
      await expect(metrics).toContainText(CHALLENGER_F1);
      await expect(proposal.getByText("Champion", { exact: true }).first()).toBeVisible();
      await expect(proposal.getByText("Challenger", { exact: true }).first()).toBeVisible();

      await expect(proposal.getByRole("heading", { name: "Report Assessments" })).toBeVisible();
      for (const name of REQUIRED_ASSESSMENTS) {
        await expect(proposal.getByText(`${name}:`)).toBeVisible();
      }
    });

    await test.step("Reject records the rejection", async () => {
      await proposal.getByRole("button", { name: "Reject" }).click();
      await expect(proposal.getByText("rejected", { exact: true })).toBeVisible();
      await expect(proposal.getByRole("button", { name: "Approve" })).toHaveCount(0);
    });

    await test.step("the Deployments page shows the environment's version", async () => {
      await page.getByRole("link", { name: "Deployments", exact: true }).click();
      await expect(page.getByRole("heading", { name: "Deployments", level: 1 })).toBeVisible();
      const table = page.getByTestId(`environments-${MODEL_NAME}`);
      const row = table.getByRole("row").filter({ hasText: "production" });
      await expect(row.getByRole("cell", { name: CHAMPION_VERSION, exact: true })).toBeVisible();
      // The rejected candidate was not promoted: no decision in the history.
      await expect(page.getByTestId("decision-row")).toHaveCount(0);
    });
  } finally {
    repo.remove();
  }
});
