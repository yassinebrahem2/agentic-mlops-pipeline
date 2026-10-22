import { expect, test } from "@playwright/test";
import { APP_NAME } from "../src/config";
import { ACCESS_TOKEN, APP_URL } from "./env";

test("the token link signs in and the session survives a reload", async ({ page }) => {
  await page.goto(`${APP_URL}/#token=${ACCESS_TOKEN}`);

  const shellTitle = page.getByRole("heading", { name: APP_NAME, exact: true });
  await expect(shellTitle).toBeVisible();
  await expect(page.getByText("Local mode access")).toHaveCount(0);
  expect(page.url()).not.toContain("token");

  await page.reload();
  await expect(shellTitle).toBeVisible();
  await expect(page.getByText("Local mode access")).toHaveCount(0);
});

test("without the token the local-mode sign-in page shows", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();

  await page.goto(`${APP_URL}/`);

  await expect(page.getByText("Local mode access")).toBeVisible();
  await expect(
    page.getByText("Open the app with the link printed in the terminal where it runs."),
  ).toBeVisible();
  await context.close();
});
