import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

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
