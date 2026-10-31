import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { FIXTURE_DIR } from "./env";

// Plain words only: the agent's answer quotes this line and the page renders it as markdown.
export const README_FIRST_LINE = "This sample project counts the words in a text file";

const README = `${README_FIRST_LINE}\n\nRun it with python count_words.py and a file name.\n`;
const PYTHON_FILE = `import sys


def count_words(text: str) -> int:
    return len(text.split())


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as handle:
        print(count_words(handle.read()))
`;

// The commit does not depend on the machine's git configuration: a fixed identity, no signing.
const GIT_IDENTITY = {
  GIT_AUTHOR_NAME: "E2E Test",
  GIT_AUTHOR_EMAIL: "e2e@example.com",
  GIT_COMMITTER_NAME: "E2E Test",
  GIT_COMMITTER_EMAIL: "e2e@example.com",
};

export interface TestRepo {
  path: string;
  remove: () => void;
}

/** A small git repository, committed on `main`, in a new temporary folder. */
export function createTestRepo(): TestRepo {
  const path = mkdtempSync(join(tmpdir(), "mlops-agents-e2e-repo-"));
  const git = (...args: string[]) => {
    execFileSync("git", args, {
      cwd: path,
      env: { ...process.env, ...GIT_IDENTITY },
      stdio: "pipe",
    });
  };

  const remove = () => {
    rmSync(path, { recursive: true, force: true });
  };

  try {
    git("init", "-b", "main");
    writeFileSync(join(path, "README.md"), README);
    writeFileSync(join(path, "count_words.py"), PYTHON_FILE);
    git("add", "README.md", "count_words.py");
    git("-c", "commit.gpgsign=false", "commit", "-m", "Add the word counter");
  } catch (error) {
    // A half-made repository is of no use: leave no folder behind and report the git failure.
    remove();
    throw error;
  }

  return { path, remove };
}

// The tables the scripted fake-onboarding model (backend/tests/fakes/onboarding_script.py) names
// in its handoff; the backend tests write the same tables with write_data_folder.
export const DATA_TABLES = ["bookings_e2e", "predictions_e2e", "labels_e2e"] as const;

const IDS = ["B001", "B002", "B003", "B004", "B005", "B006", "B007"];
const COUNTRIES = ["PRT", "GBR", "FRA", "ESP", "DEU", "PRT", "GBR"];

function csv(header: string, row: (id: string, n: number) => string): string {
  return [header, ...IDS.map((id, n) => row(id, n))].join("\n") + "\n";
}

/**
 * A team's data folder in a new folder inside the run's fixture folder (removed by e2e/run.mjs):
 * three tables of a header and 7 rows each (predictions and labels share their ID column) and
 * one unsupported file.
 */
export function createDataFolder(): string {
  const path = mkdtempSync(join(FIXTURE_DIR, "hotel-"));
  const day = (n: number) => `2026-10-${String(n + 1).padStart(2, "0")}`;
  const files: Record<(typeof DATA_TABLES)[number], string> = {
    bookings_e2e: csv(
      "booking_id,arrival_date,country,lead_time,canceled",
      (id, n) => `${id},${day(n)},${COUNTRIES[n] ?? ""},${String(10 * (n + 1))},${String(n % 2)}`,
    ),
    predictions_e2e: csv(
      "booking_id,scored_at,score",
      (id, n) => `${id},${day(n)}T06:00:00,0.${String(n + 2)}`,
    ),
    labels_e2e: csv("booking_id,canceled", (id, n) => `${id},${String(n % 2)}`),
  };
  for (const table of DATA_TABLES) {
    writeFileSync(join(path, `${table}.csv`), files[table]);
  }
  writeFileSync(join(path, "notes.txt"), "Exported every morning at 06:00.\n");
  return path;
}
