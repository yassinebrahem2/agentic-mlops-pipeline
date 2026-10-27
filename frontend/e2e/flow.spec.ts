import { randomBytes } from "node:crypto";
import { expect, test } from "@playwright/test";
import { APP_NAME } from "../src/config";
import { ACCESS_TOKEN, APP_URL, FAKE_BASE_URL } from "./env";
import { createTestRepo, README_FIRST_LINE, type TestRepo } from "./repo";

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
      "Yes",
      "Yes",
      // The fake reports its own context window; any positive size it reports will do.
      /^[1-9]\d*$/,
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

    await expect(page).toHaveURL(/\/projects\/[^/]+\/agents/);
    await expect(page.getByRole("link", { name: "Agents", exact: true })).toBeVisible();
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
      "Yes",
      "Yes",
      /^[1-9]\d*$/,
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
