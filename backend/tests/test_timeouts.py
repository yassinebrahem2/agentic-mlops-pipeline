import sys
import tomllib
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


def test_hung_test_times_out(pytester: pytest.Pytester) -> None:
    pytester.makeini(
        """
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
"""
    )
    pytester.makepyfile(
        """
import asyncio
import pytest

@pytest.fixture
async def hanging():
    yield
    await asyncio.Event().wait()

async def test_hung(hanging):
    await asyncio.Event().wait()
"""
    )
    result = pytester.runpytest_subprocess("-o", "timeout=2", "-v", timeout=60)
    output = result.stdout.str() + "\n" + result.stderr.str()
    assert "test_hung" in output
    assert result.ret != 0
    assert "Timeout" in output
    if sys.platform == "win32":
        assert "Stack of" in output


def test_docker_and_live_item_timeout_markers(pytester: pytest.Pytester) -> None:
    from tests.conftest import pytest_collection_modifyitems

    pytester.makeini(
        """
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
markers =
    docker: docker tests
    live: live tests
"""
    )
    items = pytester.getitems(
        """
import pytest

@pytest.mark.docker
def test_docker():
    pass

@pytest.mark.live
def test_live():
    pass

@pytest.mark.docker
@pytest.mark.timeout(42)
def test_docker_custom():
    pass

def test_plain():
    pass
"""
    )
    pytest_collection_modifyitems(items)

    docker_item = next(it for it in items if it.name == "test_docker")
    docker_marker = docker_item.get_closest_marker("timeout")
    assert docker_marker is not None
    assert docker_marker.args[0] == 1200

    live_item = next(it for it in items if it.name == "test_live")
    live_marker = live_item.get_closest_marker("timeout")
    assert live_marker is not None
    assert live_marker.args[0] == 600

    custom_item = next(it for it in items if it.name == "test_docker_custom")
    custom_marker = custom_item.get_closest_marker("timeout")
    assert custom_marker is not None
    assert custom_marker.args[0] == 42

    plain_item = next(it for it in items if it.name == "test_plain")
    assert plain_item.get_closest_marker("timeout") is None


def test_pytest_timeout_configuration() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    ini_options = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert ini_options.get("timeout") == 120
    assert "timeout_method" not in ini_options
