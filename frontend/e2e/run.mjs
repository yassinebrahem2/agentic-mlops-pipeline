// Runs `playwright test` and then removes the run's data folder (Decision #143) and the fixture
// folder the specs read as a team's data. Playwright's globalTeardown runs before it stops the web
// servers, so the backend would still hold the folders' files open (SQLite, DuckDB) and Windows
// would refuse the delete; once Playwright has exited, its servers are gone. Plain JavaScript
// because Node 22.17 cannot run TypeScript without a flag.
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync, realpathSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const DATA_DIR_PREFIX = "mlops-agents-e2e-";
// Not under DATA_DIR_PREFIX: each removal accepts only its own folders.
const FIXTURE_DIR_PREFIX = "mlops-agents-fixture-";

/**
 * Removes `dir` only when it resolves to a folder named with `prefix` directly inside the OS temp
 * folder; any other path is left alone and reported. Real paths are compared so a symlinked temp
 * folder (macOS) and Windows case and separators do not matter.
 * @param {string | undefined} dir
 * @param {string} prefix
 * @param {string} envName the variable the specs read the folder from, named in the reports
 * @returns {boolean} whether the folder was removed
 */
export function removeTempDir(dir, prefix, envName) {
  if (!dir) {
    console.error(`${envName} is not set; nothing removed`);
    return false;
  }
  let target;
  try {
    target = realpathSync.native(dir);
  } catch (error) {
    console.error(`Not removing ${dir}: ${String(error)}`);
    return false;
  }
  const tempRoot = realpathSync.native(tmpdir());
  if (dirname(target) !== tempRoot || !basename(target).startsWith(prefix)) {
    console.error(`Not removing ${dir}: not a ${prefix}* folder directly in ${tempRoot}`);
    return false;
  }
  rmSync(target, { recursive: true });
  return true;
}

function main() {
  const playwrightCli = createRequire(import.meta.url).resolve("@playwright/test/cli");
  const dataDir = mkdtempSync(join(tmpdir(), DATA_DIR_PREFIX));
  const fixtureDir = mkdtempSync(join(tmpdir(), FIXTURE_DIR_PREFIX));
  const env = {
    ...process.env,
    E2E_DATA_DIR: dataDir,
    E2E_FIXTURE_DIR: fixtureDir,
    E2E_ACCESS_TOKEN: randomBytes(32).toString("hex"),
  };

  // Ctrl-C reaches Playwright directly through the shared terminal; the wrapper stays alive
  // until Playwright exits, so the folders are still removed.
  const keepAlive = () => {};
  process.on("SIGINT", keepAlive);
  process.on("SIGTERM", keepAlive);

  let finished = false;
  /** @param {number} exitCode */
  const finish = (exitCode) => {
    if (finished) return;
    finished = true;
    let code = exitCode;
    /** @type {[string, string, string][]} */
    const folders = [
      [dataDir, DATA_DIR_PREFIX, "E2E_DATA_DIR"],
      [fixtureDir, FIXTURE_DIR_PREFIX, "E2E_FIXTURE_DIR"],
    ];
    // One folder that cannot be removed does not keep the other: report it, fail the run.
    for (const [dir, prefix, envName] of folders) {
      try {
        removeTempDir(dir, prefix, envName);
      } catch (error) {
        console.error(`Could not remove ${dir}: ${String(error)}`);
        code = code === 0 ? 1 : code;
      }
    }
    process.exit(code);
  };

  const child = spawn(process.execPath, [playwrightCli, "test", ...process.argv.slice(2)], {
    stdio: "inherit",
    env,
  });
  child.on("error", (error) => {
    console.error(`Could not start Playwright: ${String(error)}`);
    finish(1);
  });
  child.on("close", (code) => {
    finish(code ?? 1);
  });
}

const entry = process.argv[1];
if (entry && import.meta.url === pathToFileURL(realpathSync(entry)).href) {
  main();
}
