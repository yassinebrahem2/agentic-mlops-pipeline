import { realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname } from "node:path";

// The config names the run's data folder with this prefix; it lives here, not in env.ts, because
// the config needs it before it sets the variables that env.ts requires when it loads.
export const DATA_DIR_PREFIX = "mlops-agents-e2e-";

// Removes the data folder the config made, and nothing else: the path must resolve to a folder
// named with the prefix directly inside the OS temp folder. Real paths are compared so a
// symlinked temp folder (macOS) and Windows case and separators do not matter.
export default function globalTeardown(): void {
  const dataDir = process.env.E2E_DATA_DIR;
  if (!dataDir) {
    throw new Error("E2E_DATA_DIR is not set; refusing to remove anything");
  }
  const target = realpathSync.native(dataDir);
  const tempRoot = realpathSync.native(tmpdir());
  if (dirname(target) !== tempRoot || !basename(target).startsWith(DATA_DIR_PREFIX)) {
    throw new Error(
      `Refusing to remove ${dataDir}: not a ${DATA_DIR_PREFIX}* folder directly in ${tempRoot}`,
    );
  }
  rmSync(target, { recursive: true });
}
