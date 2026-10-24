// Runs `playwright test` and then removes the run's data folder (Decision #143). Playwright's
// globalTeardown runs before it stops the web servers, so the backend would still hold the
// folder's SQLite files open and Windows would refuse the delete; once Playwright has exited, its
// servers are gone. Plain JavaScript because Node 22.17 cannot run TypeScript without a flag.
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync, realpathSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const DATA_DIR_PREFIX = "mlops-agents-e2e-";

/**
 * Removes `dataDir` only when it resolves to a folder named with the prefix directly inside the
 * OS temp folder; any other path is left alone and reported. Real paths are compared so a
 * symlinked temp folder (macOS) and Windows case and separators do not matter.
 * @param {string | undefined} dataDir
 * @returns {boolean} whether the folder was removed
 */
export function removeDataDir(dataDir) {
  if (!dataDir) {
    console.error("E2E_DATA_DIR is not set; nothing removed");
    return false;
  }
  let target;
  try {
    target = realpathSync.native(dataDir);
  } catch (error) {
    console.error(`Not removing ${dataDir}: ${String(error)}`);
    return false;
  }
  const tempRoot = realpathSync.native(tmpdir());
  if (dirname(target) !== tempRoot || !basename(target).startsWith(DATA_DIR_PREFIX)) {
    console.error(
      `Not removing ${dataDir}: not a ${DATA_DIR_PREFIX}* folder directly in ${tempRoot}`,
    );
    return false;
  }
  rmSync(target, { recursive: true });
  return true;
}

function main() {
  const playwrightCli = createRequire(import.meta.url).resolve("@playwright/test/cli");
  const dataDir = mkdtempSync(join(tmpdir(), DATA_DIR_PREFIX));
  const env = {
    ...process.env,
    E2E_DATA_DIR: dataDir,
    E2E_ACCESS_TOKEN: randomBytes(32).toString("hex"),
  };

  // Ctrl-C reaches Playwright directly through the shared terminal; the wrapper stays alive
  // until Playwright exits, so the folder is still removed.
  const keepAlive = () => {};
  process.on("SIGINT", keepAlive);
  process.on("SIGTERM", keepAlive);

  let finished = false;
  /** @param {number} exitCode */
  const finish = (exitCode) => {
    if (finished) return;
    finished = true;
    removeDataDir(dataDir);
    process.exit(exitCode);
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
