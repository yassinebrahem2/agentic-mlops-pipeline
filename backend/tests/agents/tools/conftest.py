import subprocess
from pathlib import Path

import pytest

from mlops_agents.agents.tools.base import ToolContext
from mlops_agents.agents.tools.files import RepoFiles
from mlops_agents.integrations.github.git import GitRunner
from tests.conftest import make_repo

APP_PY = "\n".join(
    [
        "import os",
        "",
        "def train(model):",
        "    return model.fit()",
        "",
        "def evaluate(model):",
        "    return model.score()",
    ]
)


@pytest.fixture
def repo(tmp_path: Path, git_env: dict[str, str]) -> Path:
    root = make_repo(
        tmp_path / "repo",
        {
            "README.md": "# Demo\nA demo project.\n",
            "src/app.py": APP_PY,
            "src/nested/util.py": "def helper():\n    return 1\n",
            "docs/guide.md": "Train the model with train().\n",
            ".env": "API_KEY=supersecret\n",
            "keys/id_rsa": "PRIVATE KEY\n",
            "big.txt": "\n".join(f"line {i}" for i in range(1, 2501)),
            "latin1.txt": "placeholder",
        },
        git_env,
    )
    (root / "latin1.txt").write_bytes(b"caf\xe9 au lait\n")
    (root / "data.bin").write_bytes(b"\x00\x01binary")
    subprocess.run(["git", "add", "-A"], cwd=root, env=git_env, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "more"], cwd=root, env=git_env, check=True, capture_output=True
    )
    (root / "untracked.txt").write_text("not committed\n")
    return root


@pytest.fixture
def ctx(repo: Path, tmp_path: Path) -> ToolContext:
    return ToolContext(RepoFiles(repo, GitRunner(tmp_path / "bin")))
