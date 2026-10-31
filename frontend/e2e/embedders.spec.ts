import { randomBytes } from "node:crypto";
import { expect, test } from "@playwright/test";
import { APP_NAME } from "../src/config";
import { ACCESS_TOKEN, APP_URL, FAKE_BASE_URL } from "./env";
import { createTestRepo, type TestRepo } from "./repo";

let repo: TestRepo | undefined;

test.beforeAll(() => {
  repo = createTestRepo();
});

test.afterAll(() => {
  repo?.remove();
});

test("embedder settings flow: verify, app default, project override, and fallback", async ({
  page,
}) => {
  if (repo === undefined) throw new Error("beforeAll did not create the test repository");
  const repoPath = repo.path;
  const providerName = `fake-${randomBytes(3).toString("hex")}`;
  const embedAddress = `${providerName}:fake-embed`;
  const wideEmbedAddress = `${providerName}:fake-embed-wide`;
  const chatAddress = `${providerName}:fake-tools`;

  await test.step("sign in with the token link", async () => {
    await page.goto(`${APP_URL}/#token=${ACCESS_TOKEN}`);
    await expect(page.getByRole("heading", { name: APP_NAME, exact: true })).toBeVisible();
    expect(page.url()).not.toContain("token");
  });

  await test.step("registers the fake server as a self-hosted provider", async () => {
    await page.getByRole("link", { name: "AI providers" }).click();
    await expect(page.getByRole("heading", { name: "AI providers", level: 1 })).toBeVisible();

    await page.getByRole("button", { name: "Add provider" }).click();
    const providerDialog = page.getByRole("dialog", { name: "Add a provider" });
    await providerDialog.getByLabel("Name").fill(providerName);
    await providerDialog.getByRole("radio", { name: "Self-hosted" }).check();
    await providerDialog.getByLabel("Base URL").fill(FAKE_BASE_URL);
    await providerDialog.getByRole("button", { name: "Add provider" }).click();
    await expect(providerDialog).toBeHidden();
  });

  await test.step("adds fake-embed as an embedder and verifies it (dimensions show 16)", async () => {
    await page.getByRole("button", { name: "Add model" }).click();
    const modelDialog = page.getByRole("dialog", { name: "Add a model" });
    await modelDialog.getByRole("combobox", { name: "Provider" }).click();
    await page.getByRole("option", { name: providerName }).click();

    await modelDialog.getByRole("combobox", { name: "Type" }).click();
    await page.getByRole("option", { name: "Embedder" }).click();

    await modelDialog.getByRole("combobox", { name: "Model" }).click();
    await page.getByRole("option", { name: "fake-embed", exact: true }).click();
    await expect(modelDialog.getByRole("combobox", { name: "Model" })).toHaveValue("fake-embed");
    await modelDialog.getByRole("button", { name: "Add model" }).click();
    await expect(modelDialog).toBeHidden();

    await page.getByRole("button", { name: `Verify ${embedAddress}`, exact: true }).click();
    const modelRow = page.getByRole("row").filter({ hasText: embedAddress });
    await expect(modelRow.getByRole("cell")).toHaveText([
      embedAddress,
      "Embedder",
      "Not applicable",
      "Not applicable",
      "Not applicable",
      "16",
      "–",
      "Verified",
      /.+/,
      /Verify/,
    ]);
  });

  await test.step("navigates to app settings and sets fake-embed as the app default embedder", async () => {
    await page.getByRole("button", { name: "Settings" }).first().click();
    await page.getByRole("menuitem", { name: "App settings" }).click();
    await expect(page.getByRole("heading", { name: "App settings", level: 1 })).toBeVisible();

    const defaultEmbedder = page.getByRole("combobox", { name: "Default embedder" });
    await defaultEmbedder.click();
    await page.getByRole("option", { name: embedAddress }).click();
    await expect(defaultEmbedder).toHaveText(embedAddress);
  });

  let projectId: string;

  await test.step("creates a project, opens project settings, verifies using fake-embed", async () => {
    await page.getByRole("link", { name: "Projects", exact: true }).click();
    await page.getByRole("link", { name: "Add project" }).first().click();
    await expect(page.getByRole("heading", { name: "Add project", level: 1 })).toBeVisible();

    await page.getByRole("tab", { name: "Local folder" }).click();
    await expect(page.getByText("Current folder:")).toBeVisible();
    await page.getByLabel("Folder path").fill(repoPath);
    await page.getByRole("button", { name: "Go", exact: true }).click();
    await expect(page.getByText(`Current folder: ${repoPath}`)).toBeVisible();
    await page.getByRole("button", { name: "Use this folder" }).click();

    // A new project opens on its Onboarding step.
    await expect(page).toHaveURL(/\/projects\/([^/]+)\/onboarding/);
    const match = page.url().match(/\/projects\/([^/]+)/);
    if (!match) throw new Error("Could not extract projectId from URL");
    projectId = match[1];

    await page.getByRole("navigation").getByRole("link", { name: "Project settings" }).click();
    await expect(page.getByRole("heading", { name: "Project settings", level: 1 })).toBeVisible();

    const projectEmbedder = page.getByRole("combobox", { name: "Embedder for this project" });
    await expect(projectEmbedder).toHaveText("Use the app default");
    await expect(page.getByText(`Using ${embedAddress}`)).toBeVisible();
  });

  await test.step("adds fake-tools as chat model; verifies fake-tools absent from embedder and fake-embed absent from agent default", async () => {
    await page.getByRole("link", { name: "AI providers" }).click();
    await expect(page.getByRole("heading", { name: "AI providers", level: 1 })).toBeVisible();

    await page.getByRole("button", { name: "Add model" }).click();
    const chatModelDialog = page.getByRole("dialog", { name: "Add a model" });
    await chatModelDialog.getByRole("combobox", { name: "Provider" }).click();
    await page.getByRole("option", { name: providerName }).click();
    await chatModelDialog.getByRole("combobox", { name: "Model" }).click();
    await page.getByRole("option", { name: /fake-tools/ }).click();
    await expect(chatModelDialog.getByRole("combobox", { name: "Model" })).toHaveValue(
      "fake-tools",
    );
    await chatModelDialog.getByRole("button", { name: "Add model" }).click();
    await expect(chatModelDialog).toBeHidden();

    await page.getByRole("button", { name: `Verify ${chatAddress}`, exact: true }).click();
    const chatRow = page.getByRole("row").filter({ hasText: chatAddress });
    await expect(chatRow.getByText("Verified")).toBeVisible();

    // Verify fake-embed is absent from agent default model select
    const orchestratorModel = page.getByRole("combobox", { name: "Model for Orchestrator" });
    await orchestratorModel.click();
    await expect(page.getByRole("option", { name: embedAddress })).toHaveCount(0);
    await page.keyboard.press("Escape");

    // Verify fake-tools is absent from embedder select (check in app settings)
    await page.getByRole("button", { name: "Settings" }).first().click();
    await page.getByRole("menuitem", { name: "App settings" }).click();
    await expect(page.getByRole("heading", { name: "App settings", level: 1 })).toBeVisible();

    const defaultEmbedder = page.getByRole("combobox", { name: "Default embedder" });
    await defaultEmbedder.click();
    await expect(page.getByRole("option", { name: embedAddress })).toBeVisible();
    await expect(page.getByRole("option", { name: chatAddress })).toHaveCount(0);
    await page.keyboard.press("Escape");
  });

  await test.step("adds fake-embed-wide (dim 32), overrides project embedder, then clears override", async () => {
    await page.getByRole("link", { name: "AI providers" }).click();
    await expect(page.getByRole("heading", { name: "AI providers", level: 1 })).toBeVisible();

    await page.getByRole("button", { name: "Add model" }).click();
    const wideDialog = page.getByRole("dialog", { name: "Add a model" });
    await wideDialog.getByRole("combobox", { name: "Provider" }).click();
    await page.getByRole("option", { name: providerName }).click();
    await wideDialog.getByRole("combobox", { name: "Type" }).click();
    await page.getByRole("option", { name: "Embedder" }).click();
    await wideDialog.getByRole("combobox", { name: "Model" }).click();
    await page.getByRole("option", { name: "fake-embed-wide", exact: true }).click();
    await expect(wideDialog.getByRole("combobox", { name: "Model" })).toHaveValue(
      "fake-embed-wide",
    );
    await wideDialog.getByRole("button", { name: "Add model" }).click();
    await expect(wideDialog).toBeHidden();

    await page.getByRole("button", { name: `Verify ${wideEmbedAddress}`, exact: true }).click();
    const wideRow = page.getByRole("row").filter({ hasText: wideEmbedAddress });
    await expect(wideRow.getByRole("cell").nth(5)).toHaveText("32");

    // Return to project settings
    await page.goto(`${APP_URL}/projects/${projectId}/settings`);
    await expect(page.getByRole("heading", { name: "Project settings", level: 1 })).toBeVisible();

    // Override project embedder with fake-embed-wide
    const projectEmbedder = page.getByRole("combobox", { name: "Embedder for this project" });
    await projectEmbedder.click();
    await page.getByRole("option", { name: wideEmbedAddress }).click();
    await expect(projectEmbedder).toHaveText(wideEmbedAddress);
    await expect(page.getByText(`Using ${wideEmbedAddress}`)).toBeVisible();

    // Clear override back to "Use the app default"
    await projectEmbedder.click();
    await page.getByRole("option", { name: "Use the app default" }).click();
    await expect(projectEmbedder).toHaveText("Use the app default");
    await expect(page.getByText(`Using ${embedAddress}`)).toBeVisible();
  });
});
