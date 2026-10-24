import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";
// The access token and the data folder come from the environment: e2e/run.mjs (npm run e2e)
// makes them before it starts Playwright and removes the folder after Playwright exits.
import { ACCESS_TOKEN, APP_PORT, APP_URL, DATA_DIR, FAKE_BASE_URL, FAKE_PORT } from "./e2e/env";

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
