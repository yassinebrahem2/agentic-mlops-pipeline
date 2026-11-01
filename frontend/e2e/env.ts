// Values shared by playwright.config.ts and every spec. The token, the data folder and the fixture
// folder are made once by e2e/run.mjs (npm run e2e) before it starts Playwright; the runner and
// its workers inherit them through the environment.

export const APP_PORT = 8798;
export const FAKE_PORT = 8799;
export const MLFLOW_PORT = 8800;
export const SEEDED_MLFLOW_PORT = 8801;

export const APP_URL = `http://127.0.0.1:${String(APP_PORT)}`;
export const FAKE_BASE_URL = `http://127.0.0.1:${String(FAKE_PORT)}/v1`;
export const FAKE_MLFLOW_URL = `http://127.0.0.1:${String(MLFLOW_PORT)}`;
export const SEEDED_MLFLOW_URL = `http://127.0.0.1:${String(SEEDED_MLFLOW_PORT)}`;

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set; run the specs with npm run e2e`);
  }
  return value;
}

export const ACCESS_TOKEN = required("E2E_ACCESS_TOKEN");
export const DATA_DIR = required("E2E_DATA_DIR");
// Where the specs write a team's data files: never inside DATA_DIR, which connections refuse.
export const FIXTURE_DIR = required("E2E_FIXTURE_DIR");
