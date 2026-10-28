from pathlib import Path
from zipfile import ZipFile

import pytest
from build_wheel import check_wheel

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "backend/src/mlops_agents/storage/migrations"


def wheel(tmp_path: Path, *, omit: str = "", extra: str = "", tag: str = "py3-none-any") -> Path:
    members = [
        "mlops_agents/web/index.html",
        "mlops_agents/web/assets/app.js",
        "mlops_agents/agents/definitions/orchestrator.md",
        *[
            f"mlops_agents/storage/migrations/{path.relative_to(MIGRATIONS).as_posix()}"
            for path in MIGRATIONS.rglob("*.sql")
        ],
    ]
    path = tmp_path / f"mlops_agents-0.1.0-{tag}.whl"
    with ZipFile(path, "w") as archive:
        for member in members:
            if member != omit:
                archive.writestr(member, "content")
        if extra:
            archive.writestr(extra, "content")
    return path


def test_good_wheel(tmp_path: Path) -> None:
    assert check_wheel(wheel(tmp_path)) == []


@pytest.mark.parametrize(
    "member",
    [
        "mlops_agents/web/index.html",
        "mlops_agents/web/assets/app.js",
        "mlops_agents/agents/definitions/orchestrator.md",
        *[
            f"mlops_agents/storage/migrations/{path.relative_to(MIGRATIONS).as_posix()}"
            for path in MIGRATIONS.rglob("*.sql")
        ],
    ],
)
def test_missing_required_file(tmp_path: Path, member: str) -> None:
    expected = "mlops_agents/web/assets/" if "/assets/" in member else member
    assert any(expected in problem for problem in check_wheel(wheel(tmp_path, omit=member)))


@pytest.mark.parametrize(
    "member",
    [
        "tests/test_x.py",
        "mlops_agents/tests/test_x.py",
        "app.db",
        "mlops_agents/project.db",
        "keys.enc",
        "mlops_agents/cookie_secret",
        ".env",
        "mlops_agents/.env.local",
        "node_modules/pkg/index.js",
    ],
)
def test_forbidden_member(tmp_path: Path, member: str) -> None:
    assert any(member in problem for problem in check_wheel(wheel(tmp_path, extra=member)))


def test_platform_specific_tag(tmp_path: Path) -> None:
    problems = check_wheel(wheel(tmp_path, tag="cp312-cp312-win_amd64"))
    assert any("py3-none-any" in problem for problem in problems)


def test_assets_directory_is_not_an_asset(tmp_path: Path) -> None:
    path = wheel(tmp_path, omit="mlops_agents/web/assets/app.js", extra="mlops_agents/web/assets/")
    assert any("mlops_agents/web/assets/" in problem for problem in check_wheel(path))


def test_invalid_zip(tmp_path: Path) -> None:
    path = tmp_path / "mlops_agents-0.1.0-py3-none-any.whl"
    path.write_text("not a zip", encoding="utf-8")
    assert check_wheel(path)


def test_duckdb_fragment_in_built_wheel() -> None:
    assert (ROOT / "backend/src/mlops_agents/sandbox/templates/_duckdb.fragment").is_file()
