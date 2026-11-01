import { randomBytes } from "node:crypto";
import { appendFileSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "@playwright/test";
import { APP_NAME } from "../src/config";
import { ACCESS_TOKEN, APP_URL, FAKE_BASE_URL, FAKE_MLFLOW_URL, FIXTURE_DIR } from "./env";
import { createTestRepo } from "./repo";

// The scripted fake-onboarding-night model's handoff (backend/tests/e2e/fixture_project.py,
// playwright_handoff_payload) and the cause the scripted Data Quality model records
// (backend/tests/fakes/monitoring_script.py).
const BOOKINGS_TABLE = "bookings";
const ONBOARDING_COMPLETE = "Onboarding is complete.";
const SCRIPTED_CAUSE = "Data validation check failed: missing required booking values in batch";
const MIN_COLLECTION_INTERVAL_S = "10";
const SEVERITIES = ["Critical", "High", "Medium", "Low"] as const;

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

/** The fixture's data folder: the three tables the handoff names, in a folder of the run's own. */
function createNightDataFolder(): string {
  const path = mkdtempSync(join(FIXTURE_DIR, "night-"));
  for (const [table, content] of Object.entries(TABLE_FILES)) {
    writeFileSync(join(path, `${table}.csv`), `${content}\n`);
  }
  return path;
}

/** A fired agent record on `bookings`, appended to the folder the handoff reads results from. */
function appendFiredAgentRecord(dataPath: string): void {
  const folder = join(dataPath, "results");
  mkdirSync(folder, { recursive: true });
  const record = {
    check_id: "agent-bookings-check",
    origin: "agent",
    ran_at: new Date().toISOString(),
    fired: true,
    category: "data_quality",
    subject: BOOKINGS_TABLE,
    model: null,
    detail: { null_lead_times: 42 },
    source_ref: "results/agent.jsonl",
  };
  appendFileSync(join(folder, "agent.jsonl"), `${JSON.stringify(record)}\n`);
}

test("stage 4 flow: a fired check opens an incident the person sees, judges and counts", async ({
  page,
}) => {
  // Onboarding, three model checks and two 10-second collection rounds do not fit the default.
  test.setTimeout(240_000);
  const repo = createTestRepo();
  try {
    const dataPath = createNightDataFolder();
    const providerName = `fake-${randomBytes(3).toString("hex")}`;
    const address = (model: string) => `${providerName}:${model}`;
    const messages = page.getByRole("log", { name: "Messages" });
    let projectId = "";

    await test.step("sign in; the night's scripted models are added, verified and assigned", async () => {
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

      for (const model of [
        "fake-onboarding-night",
        "fake-alert-prioritization",
        "fake-data-quality",
      ]) {
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
      await assign("Onboarding & Diagnosis", "fake-onboarding-night");
      await assign("Alert Prioritization", "fake-alert-prioritization");
      await assign("Data Quality", "fake-data-quality");
    });

    await test.step("add the repository and onboard with the scripted night handoff", async () => {
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
      await expect(page.getByText(`Ready: ${address("fake-onboarding-night")}`)).toBeVisible();
      await page.getByRole("button", { name: "Start", exact: true }).click();

      await expect(page).toHaveURL(/\/agents\?agent=onboarding/);
      await expect(messages.getByText(/DATA_PATH=/)).toBeVisible();
      await page
        .getByLabel("Message Onboarding & Diagnosis")
        .fill(`DATA_PATH=${dataPath}\nMLFLOW_URL=${FAKE_MLFLOW_URL}`);
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

    await test.step("set the collection interval to its minimum in the project settings", async () => {
      await page.getByRole("link", { name: "Project settings", exact: true }).click();
      await expect(page).toHaveURL(/\/projects\/[^/]+\/settings/);
      const interval = page.getByLabel("Collection interval (seconds)");
      await interval.fill(MIN_COLLECTION_INTERVAL_S);
      await page.getByRole("button", { name: "Save monitoring" }).click();
      await expect(page.getByRole("status").filter({ hasText: "Saved." })).toBeVisible();
      await expect(interval).toHaveValue(MIN_COLLECTION_INTERVAL_S);
    });

    await test.step("a fired agent record arrives: the Inbox lists the incident with its severity", async () => {
      appendFiredAgentRecord(dataPath);
      await page.getByRole("link", { name: "Inbox", exact: true }).click();
      const incidents = page.getByRole("region", { name: "Incidents" });
      const row = incidents.getByRole("listitem").filter({ hasText: "Data quality" });
      // The next collection (every 10 seconds) stores the record and opens the incident.
      await expect(row).toHaveCount(1, { timeout: 60_000 });
      await expect(row.getByText(new RegExp(`^(${SEVERITIES.join("|")})$`))).toBeVisible();
      await expect(row.getByText("Open", { exact: true })).toBeVisible();
      // Data Quality was woken by the incident and recorded the scripted cause.
      await expect(row.getByRole("link", { name: SCRIPTED_CAUSE })).toBeVisible({
        timeout: 60_000,
      });
    });

    await test.step("the incident page shows the scripted cause on its timeline", async () => {
      await page.getByRole("link", { name: SCRIPTED_CAUSE }).click();
      await expect(page).toHaveURL(/\/projects\/[^/]+\/incidents\/[^/]+/);
      await expect(page.getByRole("heading", { name: SCRIPTED_CAUSE, level: 1 })).toBeVisible();

      const timeline = page.getByTestId("incident-timeline");
      await expect(timeline.getByText("Incident opened")).toBeVisible();
      const updated = timeline.getByRole("listitem").filter({ hasText: "Incident updated" });
      await updated.getByText("Details").click();
      await expect(updated.getByText(SCRIPTED_CAUSE)).toBeVisible();
    });

    await test.step("a Real issue verdict is recorded", async () => {
      await page.getByRole("button", { name: "Real issue" }).click();
      const verdict = page.getByTestId("incident-verdict");
      await expect(verdict.getByText("Real issue")).toBeVisible();
      await expect(
        page.getByTestId("incident-timeline").getByText("Verdict recorded"),
      ).toBeVisible();
    });

    await test.step("the Dashboard counts the open incident", async () => {
      await page.getByRole("link", { name: "Dashboard", exact: true }).click();
      const counts = page.getByRole("list", { name: "Active incidents by severity" });
      await expect(counts).toBeVisible();
      // One open incident in total, whichever severity the night gave it: a row reads "High1".
      await expect
        .poll(async () => {
          const rows = await counts.getByRole("listitem").allTextContents();
          return rows.map((text) => Number(/\d+$/.exec(text)?.[0] ?? Number.NaN));
        })
        .toEqual(expect.arrayContaining([1]));
      const rows = await counts.getByRole("listitem").allTextContents();
      const total = rows.reduce((sum, text) => sum + Number(/\d+$/.exec(text)?.[0] ?? 0), 0);
      expect(total).toBe(1);
    });
  } finally {
    repo.remove();
  }
});
