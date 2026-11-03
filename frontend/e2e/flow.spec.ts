import { randomBytes } from "node:crypto";
import { basename } from "node:path";
import { expect, test } from "@playwright/test";
import { APP_NAME } from "../src/config";
import { ACCESS_TOKEN, APP_URL, FAKE_BASE_URL, FAKE_MLFLOW_URL } from "./env";
import {
  createDataFolder,
  createTestRepo,
  DATA_TABLES,
  README_FIRST_LINE,
  type TestRepo,
} from "./repo";

let repo: TestRepo | undefined;

test.beforeAll(() => {
  repo = createTestRepo();
});

test.afterAll(() => {
  repo?.remove();
});

test("stage 1 flow: a provider, a local project and a question the agent answers", async ({
  page,
}) => {
  if (repo === undefined) throw new Error("beforeAll did not create the test repository");
  const repoPath = repo.path;
  // The specs share one app and database, so this run's provider gets a name of its own.
  const providerName = `fake-${randomBytes(3).toString("hex")}`;
  const modelAddress = `${providerName}:fake-tools`;
  const writeModelAddress = `${providerName}:fake-write`;

  await test.step("sign in with the token link", async () => {
    await page.goto(`${APP_URL}/#token=${ACCESS_TOKEN}`);
    await expect(page.getByRole("heading", { name: APP_NAME, exact: true })).toBeVisible();
    expect(page.url()).not.toContain("token");
  });

  await test.step("add the fake as a self-hosted provider and make fake-tools the Orchestrator's model", async () => {
    await page.getByRole("link", { name: "AI providers" }).click();
    await expect(page.getByRole("heading", { name: "AI providers", level: 1 })).toBeVisible();

    await page.getByRole("button", { name: "Add provider" }).click();
    const providerDialog = page.getByRole("dialog", { name: "Add a provider" });
    await providerDialog.getByLabel("Name").fill(providerName);
    await providerDialog.getByRole("radio", { name: "Self-hosted" }).check();
    await providerDialog.getByLabel("Base URL").fill(FAKE_BASE_URL);
    await providerDialog.getByRole("button", { name: "Add provider" }).click();
    await expect(providerDialog).toBeHidden();

    await page.getByRole("button", { name: "Add model" }).click();
    const modelDialog = page.getByRole("dialog", { name: "Add a model" });
    await modelDialog.getByRole("combobox", { name: "Provider" }).click();
    await page.getByRole("option", { name: providerName }).click();
    await modelDialog.getByRole("combobox", { name: "Model" }).click();
    await page.getByRole("option", { name: /fake-tools/ }).click();
    await expect(modelDialog.getByRole("combobox", { name: "Model" })).toHaveValue("fake-tools");
    await modelDialog.getByRole("button", { name: "Add model" }).click();
    await expect(modelDialog).toBeHidden();

    // Verification is a real call to the fake: wait for the row to show its answer.
    await page.getByRole("button", { name: `Verify ${modelAddress}`, exact: true }).click();
    const modelRow = page.getByRole("row").filter({ hasText: modelAddress });
    await expect(modelRow.getByRole("cell")).toHaveText([
      modelAddress,
      "Chat",
      "Yes",
      "Yes",
      // The fake reports its own context window; any positive size it reports will do.
      /^[1-9]\d*$/,
      /None|Set/,
      "Not applicable",
      /No price/,
      "Verified",
      /.+/,
      /Verify/,
    ]);

    const orchestratorModel = page.getByRole("combobox", { name: "Model for Orchestrator" });
    await orchestratorModel.click();
    await page.getByRole("option", { name: modelAddress }).click();
    await expect(orchestratorModel).toHaveText(modelAddress);
    await expect(orchestratorModel).toBeEnabled();
  });

  await test.step("add the repository as a local project; it opens", async () => {
    await page.getByRole("link", { name: "Projects", exact: true }).click();
    // An empty project list shows a second "Add project" link; the header's comes first.
    await page.getByRole("link", { name: "Add project" }).first().click();
    await expect(page.getByRole("heading", { name: "Add project", level: 1 })).toBeVisible();

    await page.getByRole("tab", { name: "Local folder" }).click();
    // The browser fills the path box with the folder it lists first; type over it once it has.
    await expect(page.getByText("Current folder:")).toBeVisible();
    await page.getByLabel("Folder path").fill(repoPath);
    await page.getByRole("button", { name: "Go", exact: true }).click();
    await expect(page.getByText(`Current folder: ${repoPath}`)).toBeVisible();
    await page.getByRole("button", { name: "Use this folder" }).click();

    await expect(page).toHaveURL(/\/projects\/[^/]+\/onboarding/);
    // Only the Orchestrator has a model here, so Onboarding's Start stays disabled; go on to Agents.
    await page.getByRole("link", { name: "Agents", exact: true }).click();
    await expect(page).toHaveURL(/\/projects\/[^/]+\/agents/);
  });

  const messages = page.getByRole("log", { name: "Messages" });
  const readStep = messages.getByRole("button", { name: /Read.*README\.md/ });
  // The answer is markdown text; the tool's own output sits in a code block, not a paragraph.
  const answer = messages.getByRole("paragraph").filter({ hasText: README_FIRST_LINE });

  await test.step("ask about the README: a Read step on README.md, then the answer", async () => {
    await page.getByLabel("Message Orchestrator").fill("What does the README say?");
    await page.getByRole("button", { name: "Send", exact: true }).click();

    await expect(readStep).toBeVisible();
    await readStep.click();
    await expect(readStep).toHaveAttribute("aria-expanded", "true");
    await expect(messages.getByRole("code").filter({ hasText: README_FIRST_LINE })).toBeVisible();

    await expect(answer).toBeVisible();
    await expect(messages.getByRole("status", { name: "Typing..." })).toHaveCount(0);
  });

  await test.step("reload: the conversation is still there, tool step included", async () => {
    await page.reload();

    await expect(messages.getByText("What does the README say?")).toBeVisible();
    await expect(answer).toBeVisible();
    await expect(readStep).toBeVisible();
    await readStep.click();
    await expect(messages.getByRole("code").filter({ hasText: README_FIRST_LINE })).toBeVisible();
  });

  await test.step("the approval flow: StartTask and Write approved, task bar shows the task", async () => {
    // 1. Level ask: default level, bypass warning banner is absent
    await expect(page.getByText("Bypass mode: agents act without asking.")).toHaveCount(0);

    // 2. Configure fake-write as the Orchestrator's model
    await page.getByRole("link", { name: "AI providers" }).click();
    await expect(page.getByRole("heading", { name: "AI providers", level: 1 })).toBeVisible();

    await page.getByRole("button", { name: "Add model" }).click();
    const modelDialog = page.getByRole("dialog", { name: "Add a model" });
    await modelDialog.getByRole("combobox", { name: "Provider" }).click();
    await page.getByRole("option", { name: providerName }).click();
    await modelDialog.getByRole("combobox", { name: "Model" }).click();
    await page.getByRole("option", { name: /fake-write/ }).click();
    await expect(modelDialog.getByRole("combobox", { name: "Model" })).toHaveValue("fake-write");
    await modelDialog.getByRole("button", { name: "Add model" }).click();
    await expect(modelDialog).toBeHidden();

    await page.getByRole("button", { name: `Verify ${writeModelAddress}`, exact: true }).click();
    const writeModelRow = page.getByRole("row").filter({ hasText: writeModelAddress });
    await expect(writeModelRow.getByRole("cell")).toHaveText([
      writeModelAddress,
      "Chat",
      "Yes",
      "Yes",
      /^[1-9]\d*$/,
      /None|Set/,
      "Not applicable",
      /No price/,
      "Verified",
      /.+/,
      /Verify/,
    ]);

    const orchestratorModel = page.getByRole("combobox", { name: "Model for Orchestrator" });
    await orchestratorModel.click();
    await page.getByRole("option", { name: writeModelAddress }).click();
    await expect(orchestratorModel).toHaveText(writeModelAddress);
    await expect(orchestratorModel).toBeEnabled();

    // Return to the project
    await page.getByRole("link", { name: "Agents", exact: true }).click();
    await expect(page).toHaveURL(/\/projects\/[^/]+\/agents/);

    // 3. Ask the Orchestrator on fake-write
    await page.getByLabel("Message Orchestrator").fill("Please start a task and write notes.");
    await page.getByRole("button", { name: "Send", exact: true }).click();

    // 4. An approval card for StartTask appears; approve it.
    await expect(page.getByText("StartTask").first()).toBeVisible();
    await page.getByRole("button", { name: "Approve" }).click();

    // 5. A card with the NOTES.md diff appears; approve it.
    await expect(page.getByText("Write NOTES.md")).toBeVisible();
    await expect(page.getByRole("region", { name: "Diff view" })).toBeVisible();
    await page.getByRole("button", { name: "Approve" }).click();

    // 6. The agent gives its final reply, the turn ends, and the task bar shows the task.
    await expect(
      messages.getByText("I have started the task and written NOTES.md.", { exact: true }),
    ).toBeVisible();
    await expect(messages.getByRole("status", { name: "Typing..." })).toHaveCount(0);
    await expect(page.getByText("Waiting for your approval")).toHaveCount(0);

    const taskBar = page.getByRole("region", { name: "Current task" });
    await expect(taskBar).toBeVisible();
    await expect(taskBar.getByText("Write notes")).toBeVisible();
    await expect(taskBar.getByText("local review")).toBeVisible();
    await expect(taskBar.getByRole("button", { name: "Discard task" })).toBeVisible();
  });
});

// The scripted fake-onboarding model's handoff (backend/tests/fakes/onboarding_script.py): the
// values the cards and the API show back.
const CONNECTION_NAME = "hotel-data";
const MODEL_NAME = "hotel-cancel";
const FEED_TIMEZONE = "Europe/Lisbon";
const ON_CALL_TIMEZONE = "Europe/Madrid";
const ON_CALL_HOURS = "08:30 - 17:30";
const ONBOARDING_COMPLETE = "Onboarding is complete.";
const AUDIT_CONTEXT = "Audit context included:";
const AUDIT_AGENTS = ["Data Quality", "Model Performance", "Retraining", "Deployment & Rollback"];

test("stage 3 flow: Onboarding proposes the connection and the handoff, and the audits start", async ({
  page,
}) => {
  const onboardingRepo = createTestRepo();
  try {
    const projectName = basename(onboardingRepo.path);
    const dataPath = createDataFolder();
    const providerName = `fake-${randomBytes(3).toString("hex")}`;
    const onboardingAddress = `${providerName}:fake-onboarding`;
    const auditAddress = `${providerName}:fake-audit`;
    let projectId = "";

    await test.step("sign in; the fake's two scripted models are added, verified and assigned", async () => {
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

      for (const model of ["fake-onboarding", "fake-audit"]) {
        await page.getByRole("button", { name: "Add model" }).click();
        const modelDialog = page.getByRole("dialog", { name: "Add a model" });
        await modelDialog.getByRole("combobox", { name: "Provider" }).click();
        await page.getByRole("option", { name: providerName }).click();
        await modelDialog.getByRole("combobox", { name: "Model" }).click();
        await page.getByRole("option", { name: new RegExp(`^${model}(?![-\\w])`) }).click();
        await expect(modelDialog.getByRole("combobox", { name: "Model" })).toHaveValue(model);
        await modelDialog.getByRole("button", { name: "Add model" }).click();
        await expect(modelDialog).toBeHidden();

        const address = `${providerName}:${model}`;
        await page.getByRole("button", { name: `Verify ${address}`, exact: true }).click();
        const row = page.getByRole("row").filter({ hasText: address });
        await expect(row.getByText("Verified")).toBeVisible();
      }

      const assign = async (agent: string, address: string) => {
        const combobox = page.getByRole("combobox", { name: `Model for ${agent}` });
        await combobox.click();
        await page.getByRole("option", { name: address, exact: true }).click();
        await expect(combobox).toHaveText(address);
      };
      await assign("Onboarding & Diagnosis", onboardingAddress);
      for (const agent of AUDIT_AGENTS) {
        await assign(agent, auditAddress);
      }
    });

    const messages = page.getByRole("log", { name: "Messages" });

    await test.step("add the repository: the Onboarding step starts the session", async () => {
      await page.getByRole("link", { name: "Projects", exact: true }).click();
      await page.getByRole("link", { name: "Add project" }).first().click();
      await page.getByRole("tab", { name: "Local folder" }).click();
      await expect(page.getByText("Current folder:")).toBeVisible();
      await page.getByLabel("Folder path").fill(onboardingRepo.path);
      await page.getByRole("button", { name: "Go", exact: true }).click();
      await expect(page.getByText(`Current folder: ${onboardingRepo.path}`)).toBeVisible();
      await page.getByRole("button", { name: "Use this folder" }).click();

      await expect(page).toHaveURL(/\/projects\/[^/]+\/onboarding/);
      projectId = /\/projects\/([^/]+)\//.exec(page.url())?.[1] ?? "";
      expect(projectId).not.toBe("");
      await expect(page.getByText(`Ready: ${onboardingAddress}`)).toBeVisible();
      await page.getByRole("button", { name: "Start", exact: true }).click();

      await expect(page).toHaveURL(/\/agents\?agent=onboarding/);
      // The wizard's message names the project; the fake answers by asking for DATA_PATH=.
      await expect(messages.getByText(projectName)).toBeVisible();
      await expect(messages.getByText(/DATA_PATH=/)).toBeVisible();
      await expect(messages.getByRole("status", { name: "Typing..." })).toHaveCount(0);
    });

    await test.step("approve the connection, then the handoff", async () => {
      await page
        .getByLabel("Message Onboarding & Diagnosis")
        .fill(`DATA_PATH=${dataPath}\nMLFLOW_URL=${FAKE_MLFLOW_URL}`);
      await page.getByRole("button", { name: "Send", exact: true }).click();

      const card = (kind: string) =>
        messages
          .locator('[data-testid^="approval-card-"]')
          .filter({ hasText: `Proposal: ${kind}` });
      const connectionsCard = card("connections");
      await expect(connectionsCard.getByText(CONNECTION_NAME, { exact: true })).toBeVisible();
      await expect(connectionsCard.getByText(dataPath)).toBeVisible();
      await connectionsCard.getByRole("button", { name: "Approve" }).click();

      // The resumed turn proposes the handoff.
      const handoffCard = card("handoff");
      const models = handoffCard.getByRole("region", { name: "Models" });
      await expect(models.getByText(MODEL_NAME, { exact: true })).toBeVisible();
      await expect(models.getByText("team_gate", { exact: true })).toBeVisible();
      const roles = handoffCard.getByRole("region", { name: "Data Roles" });
      for (const table of DATA_TABLES) {
        await expect(roles.getByRole("cell", { name: table, exact: true })).toBeVisible();
      }
      const tracking = handoffCard.getByRole("region", { name: "Tracking" });
      await expect(tracking.getByText(FAKE_MLFLOW_URL)).toBeVisible();
      const operations = handoffCard.getByRole("region", { name: "Operations" });
      // The feed's line and the on-call line, each with its own time zone.
      await expect(
        operations.getByText(new RegExp(String.raw`Grace: \d+m \| Timezone: ${FEED_TIMEZONE}`)),
      ).toBeVisible();
      await expect(
        operations.getByText(`(${ON_CALL_HOURS}) | Timezone: ${ON_CALL_TIMEZONE}`),
      ).toBeVisible();
      await handoffCard.getByRole("button", { name: "Approve" }).click();

      await expect(messages.getByText(ONBOARDING_COMPLETE, { exact: true })).toBeVisible();
      await expect(messages.getByRole("status", { name: "Typing..." })).toHaveCount(0);
    });

    await test.step("the API shows the connection, the tracking source and the handoff", async () => {
      const api = `${APP_URL}/api/projects/${projectId}`;
      const connections = await page.request.get(`${api}/connections`);
      expect(connections.ok()).toBe(true);
      expect(await connections.json()).toEqual([
        expect.objectContaining({ name: CONNECTION_NAME, status: "ok" }),
      ]);

      // The first sync of the new source succeeds against the fake MLflow server.
      await expect
        .poll(async () => (await page.request.get(`${api}/tracking/sources`)).json() as unknown, {
          timeout: 30_000,
        })
        .toEqual([
          expect.objectContaining({
            type: "mlflow",
            settings: { url: FAKE_MLFLOW_URL },
            last_sync: expect.any(String),
            last_error: null,
          }),
        ]);

      const handoff = await page.request.get(`${api}/handoff`);
      expect(handoff.ok()).toBe(true);
      const body = (await handoff.json()) as { payload: { models: { name: string }[] } };
      expect(body.payload.models[0]?.name).toBe(MODEL_NAME);
    });

    await test.step("the audits start with the data summary; Onboarding gets no kickoff", async () => {
      // A fresh load per agent, so no assertion can pass on the previous session's messages.
      const open = async (agent: string, displayName: string) => {
        await page.goto(`${APP_URL}/projects/${projectId}/agents?agent=${agent}`);
        await expect(page.getByLabel(`Message ${displayName}`)).toBeVisible();
      };
      // Two of the four audit agents whose definitions name the data_summary hook.
      for (const [agent, displayName] of [
        ["data-quality", "Data Quality"],
        ["model-performance", "Model Performance"],
      ] as const) {
        await open(agent, displayName);
        await expect(messages.getByText("Handoff", { exact: true })).toBeVisible();
        const reply = messages.getByText(AUDIT_CONTEXT);
        await expect(reply).toBeVisible();
        await expect(reply).toContainText(`Table "${DATA_TABLES[0]}"`);
      }

      await open("onboarding", "Onboarding & Diagnosis");
      await expect(messages.getByText(ONBOARDING_COMPLETE, { exact: true })).toBeVisible();
      await expect(messages.getByText("Handoff", { exact: true })).toHaveCount(0);
    });
  } finally {
    onboardingRepo.remove();
  }
});
