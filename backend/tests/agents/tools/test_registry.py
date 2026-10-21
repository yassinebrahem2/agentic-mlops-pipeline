from mlops_agents.agents.tools import default_registry


def test_default_registry_has_the_stage_1_tools() -> None:
    assert default_registry().names() == frozenset({"Read", "Glob", "Grep"})
