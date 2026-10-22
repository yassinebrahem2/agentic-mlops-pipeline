import { randomBytes } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

// Playwright loads this file in the runner and again in every worker; workers inherit the
// runner's environment, so the token and the data folder are made once and then reused.
process.env.E2E_ACCESS_TOKEN ??= randomBytes(32).toString("hex");
process.env.E2E_DATA_DIR ??= mkdtempSync(join(tmpdir(), "mlops-agents-e2e-"));

// Imported after the two variables above are set, because env.ts reads them when it loads.
const { ACCESS_TOKEN, APP_PORT, APP_URL, DATA_DIR, FAKE_BASE_URL, FAKE_PORT } =
  await import("./e2e/env");

// Both servers run from the backend folder, so the plain `uv run ...` commands work on every OS.
const backendDir = fileURLToPath(new URL("../backend", import.meta.url));
const startupTimeout = 60_000;

export default defineConfig({
  testDir: "./e2e",
  // The specs share one app and one database, and the flow builds up state step by step.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: APP_URL,
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `uv run python -m tests.fakes.fake_openai --port ${String(FAKE_PORT)}`,
      cwd: backendDir,
      url: `${FAKE_BASE_URL}/models`,
      reuseExistingServer: false,
      timeout: startupTimeout,
    },
    {
      command: `uv run mlops-agents start --no-browser --port ${String(APP_PORT)}`,
      cwd: backendDir,
      url: `${APP_URL}/api/auth/status`,
      reuseExistingServer: false,
      timeout: startupTimeout,
      env: {
        MLOPS_AGENTS_HOME: DATA_DIR,
        MLOPS_AGENTS_ACCESS_TOKEN: ACCESS_TOKEN,
      },
    },
  ],
});
