"""The builder's injection point: which hooks run, and with which context."""

from __future__ import annotations

from pathlib import Path

import httpx2

from mlops_agents.agents.hooks.contract import HookContext
from mlops_agents.agents.hooks.registry import HookRegistry
from mlops_agents.paths import DataPaths
from mlops_agents.storage.databases import Databases
from tests.agents.runtime.builder_env import BuilderEnv, build_builder_env, start_turn
from tests.conftest import MemoryKeyStore
from tests.fakes.fake_openai import RunningFake


class SpyHook:
    def __init__(self, name: str = "spy", text: str = "spy text") -> None:
        self.name = name
        self.text = text
        self.seen: list[HookContext] = []

    async def render(self, ctx: HookContext) -> str:
        self.seen.append(ctx)
        return self.text


def _definitions(tmp_path: Path) -> Path:
    folder = tmp_path / "defs"
    folder.mkdir()
    (folder / "helper.md").write_text(
        "---\nname: helper\ndisplay_name: Helper\ndescription: Helps.\n"
        "tools: [Read]\ncontext_hooks: [spy]\n---\nYou help.\n",
        encoding="utf-8",
    )
    (folder / "plain.md").write_text(
        "---\nname: plain\ndisplay_name: Plain\ndescription: No hooks.\n"
        "tools: [Read]\n---\nYou are plain.\n",
        encoding="utf-8",
    )
    return folder


async def _env(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
    spy: SpyHook,
) -> BuilderEnv:
    hooks = HookRegistry()
    hooks.register(spy)
    env = await build_builder_env(
        tmp_path,
        paths,
        databases,
        keystore,
        fake_openai,
        model_http,
        git_env,
        hooks=hooks,
        extra_dirs=[_definitions(tmp_path)],
    )
    for agent in ("helper", "plain"):
        await env.settings.set_default_model(agent, f"{env.provider}:fake-tools")
    return env


async def test_hooks_run_with_the_project_the_agent_and_the_trigger_text(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    spy = SpyHook()
    env = await _env(tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, spy)
    session_id, turn_id = await start_turn(
        env.store, env.project_id, "helper", "Profile the ", "orders table"
    )

    agent = await env.builder.build(
        env.project_id, "helper", session_id=session_id, turn_id=turn_id
    )

    assert spy.seen == [
        HookContext(
            project_id=env.project_id, agent="helper", trigger_text="Profile the orders table"
        )
    ]
    assert agent.hook_context == '<data source="hook:spy">\nspy text\n</data>'


async def test_a_build_without_the_turn_ids_runs_no_hooks(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    spy = SpyHook()
    env = await _env(tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, spy)
    session_id, turn_id = await start_turn(env.store, env.project_id, "helper", "Hi")

    assert (await env.builder.build(env.project_id, "helper")).hook_context is None
    assert (
        await env.builder.build(env.project_id, "helper", session_id=session_id)
    ).hook_context is None
    assert (await env.builder.build(env.project_id, "helper", turn_id=turn_id)).hook_context is None
    assert spy.seen == []


async def test_the_trigger_text_is_empty_when_the_trigger_has_none(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    spy = SpyHook()
    env = await _env(tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, spy)
    session_id, turn_id = await start_turn(env.store, env.project_id, "helper")
    other_session, _ = await start_turn(env.store, env.project_id, "plain", "Not yours")

    await env.builder.build(env.project_id, "helper", session_id=session_id, turn_id=turn_id)
    # The trigger is looked up in the turn's own session, so another session finds none.
    await env.builder.build(env.project_id, "helper", session_id=other_session, turn_id=turn_id)

    assert [ctx.trigger_text for ctx in spy.seen] == ["", ""]


async def test_an_agent_without_hooks_calls_none(
    tmp_path: Path,
    paths: DataPaths,
    databases: Databases,
    keystore: MemoryKeyStore,
    fake_openai: RunningFake,
    model_http: httpx2.AsyncClient,
    git_env: dict[str, str],
) -> None:
    spy = SpyHook()
    env = await _env(tmp_path, paths, databases, keystore, fake_openai, model_http, git_env, spy)
    session_id, turn_id = await start_turn(env.store, env.project_id, "plain", "Hi")

    agent = await env.builder.build(env.project_id, "plain", session_id=session_id, turn_id=turn_id)

    assert agent.hook_context is None
    assert spy.seen == []
