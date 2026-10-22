"""Build the frontend and a portable wheel, then verify its shipped resources."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
MIGRATIONS = BACKEND / "src/mlops_agents/storage/migrations"


def check_wheel(path: Path) -> list[str]:
    """Return missing resources, forbidden members and incompatible wheel tags."""
    problems: list[str] = []
    if not path.name.endswith("-py3-none-any.whl"):
        problems.append(f"{path.name}: wheel tag must be py3-none-any")
    required = {
        "mlops_agents/web/index.html",
        "mlops_agents/agents/definitions/orchestrator.md",
        *[
            f"mlops_agents/storage/migrations/{migration.relative_to(MIGRATIONS).as_posix()}"
            for migration in MIGRATIONS.rglob("*.sql")
        ],
    }
    try:
        with ZipFile(path) as archive:
            files = {entry.filename for entry in archive.infolist() if not entry.is_dir()}
            for missing in sorted(required - files):
                problems.append(f"Missing required file: {missing}")
            if not any(name.startswith("mlops_agents/web/assets/") for name in files):
                problems.append("Missing built asset under mlops_agents/web/assets/")
            for name in archive.namelist():
                parts = PurePosixPath(name).parts
                if any(
                    part.casefold() in {"tests", "node_modules", "keys.enc", "cookie_secret"}
                    or part.casefold().endswith(".db")
                    or part.casefold().startswith(".env")
                    for part in parts
                ):
                    problems.append(f"Forbidden wheel member: {name}")
    except (OSError, BadZipFile) as error:
        problems.append(f"Cannot read wheel {path}: {error}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-npm-ci", action="store_true", help="Reuse current node_modules")
    args = parser.parse_args()
    # Resolve the executable, including npm.cmd on Windows, before invoking it.
    npm = shutil.which("npm")
    uv = shutil.which("uv")
    if npm is None:
        print("npm was not found on PATH. Install Node 22.", file=sys.stderr)
        return 1
    if uv is None:
        print("uv was not found on PATH. Install uv.", file=sys.stderr)
        return 1

    dist = BACKEND / "dist"
    try:
        # Refuse redirects before touching outputs; only direct wheel files are removed.
        if dist.resolve() != BACKEND / "dist":
            raise ValueError(f"Refusing to clean redirected output directory: {dist}")
        dist.mkdir(exist_ok=True)
        for old_wheel in dist.glob("*.whl"):
            if old_wheel.is_file() or old_wheel.is_symlink():
                old_wheel.unlink()

        steps: list[tuple[str, list[str], Path]] = []
        if not args.skip_npm_ci:
            steps.append(("npm ci", [npm, "ci"], ROOT / "frontend"))
        steps.extend(
            [
                ("npm run build", [npm, "run", "build"], ROOT / "frontend"),
                ("uv build --wheel", [uv, "build", "--wheel"], BACKEND),
            ]
        )
        for name, command, cwd in steps:
            result = subprocess.run(command, cwd=cwd, check=False)
            if result.returncode:
                print(f"{name} failed with exit code {result.returncode}", file=sys.stderr)
                return 1

        wheels = sorted(dist.glob("*.whl"))
        if len(wheels) != 1:
            print(f"Expected one wheel in {dist}, found {len(wheels)}", file=sys.stderr)
            return 1
        problems = check_wheel(wheels[0])
        if problems:
            print("Wheel validation failed:\n" + "\n".join(problems), file=sys.stderr)
            return 1
        print(wheels[0])
        return 0
    except (OSError, ValueError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
