# pyright: reportPrivateUsage=false
"""Unit tests for conftest.py keystore parameter, harness.py, fixture_project.py,
monitoring_script.py, and fake_openai.py integration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import mlops_agents.integrations.github.api as gh_api
from mlops_agents.agents.tools.names import (
    INCIDENT_UPDATE,
    MONITORING_QUERY,
    RUN_HOOK,
)
from mlops_agents.agents.tools.propose import PROPOSE_TOOL_NAME
from mlops_agents.monitoring.expr import Compare
from mlops_agents.monitoring.incidents.grouping import is_downstream
from mlops_agents.proposals.payloads import HandoffPayload
from tests.api.conftest import add_local_project, started_app
from tests.conftest import MemoryKeyStore
from tests.e2e.fixture_project import (
    AGENT_CHECK_ID,
    MODEL_NAME,
    ROLLBACK_HOOK_ID,
    ROLLBACK_RULE_ID,
    SCORING_WORKFLOW,
    SECOND_DEADLINE_MINUTES,
    TABLE_BOOKINGS,
    TABLE_PREDICTIONS,
    TABLE_WEATHER,
    TEAM_CHECK_ID,
    TEAM_FEED_NAME,
    WEATHER_FEED_NAME,
    create_fixture_repo,
    handoff_payload,
    monitoring_settings_payload,
    playwright_handoff_payload,
    rollback_command_spec,
    rollback_rule_spec,
    write_fixture_data_folder,
    write_results_record,
)
from tests.e2e.harness import (
    SettableClock,
    create_e2e_harness,
    run_due,
    wait_for_agent_idle,
)
from tests.fakes.fake_openai import MODELS, FakeState, _reply
from tests.fakes.monitoring_script import (
    ASK_FOR_DATA,
    DATA_PATH_KEY,
    MLFLOW_URL_KEY,
    ONBOARDING_COMPLETE,
    SCRIPTED_DATA_QUALITY_CAUSE,
    alert_prioritization_reply,
    data_quality_reply,
    deployment_rollback_reply,
    onboarding_night_reply,
)


@pytest.mark.asyncio
async def test_conftest_keystore_parameter(tmp_path: Path) -> None:
    """Verify build_app and started_app accept and use the custom keystore parameter."""
    custom_keystore = MemoryKeyStore()
    custom_keystore.set("discord_webhook", "https://discord.example.com/webhook")

    async with started_app(tmp_path / "home1", keystore=custom_keystore) as running_app:
        assert running_app.state.services.keystore is custom_keystore
        assert (
            running_app.state.services.keystore.get("discord_webhook")
            == "https://discord.example.com/webhook"
        )

    # When None, default to a fresh MemoryKeyStore
    async with started_app(tmp_path / "home2", keystore=None) as running_app_default:
        assert isinstance(running_app_default.state.services.keystore, MemoryKeyStore)
        assert running_app_default.state.services.keystore.get("discord_webhook") is None


def test_settable_clock() -> None:
    """SettableClock supports instant inspection, advance, set, and non-blocking sleep."""
    start = datetime(2026, 10, 31, 10, 0, 0, tzinfo=UTC)
    clock = SettableClock(start=start)
    assert clock.now() == start

    clock.advance(120.0)
    assert clock.now() == start + timedelta(seconds=120)

    clock.advance(timedelta(minutes=5))
    assert clock.now() == start + timedelta(seconds=120, minutes=5)

    target = datetime(2026, 11, 1, 8, 30, 0, tzinfo=UTC)
    clock.set(target)
    assert clock.now() == target


@pytest.mark.asyncio
async def test_harness_lifecycle_and_restart(tmp_path: Path, git_env: dict[str, str]) -> None:
    """create_e2e_harness starts fake servers, patches GitHub API, and preserves state on restart."""
    home = tmp_path / "e2e-data"
    start_time = datetime(2026, 10, 31, 1, 0, 0, tzinfo=UTC)

    async with create_e2e_harness(home, start_time=start_time) as harness:
        assert harness.clock.now() == start_time
        assert harness.fake_github.base_url == gh_api.GITHUB_API

        # Start first app instance
        async with harness.started_app() as app1:
            client1 = harness.client(app1)
            repo_path = create_fixture_repo(tmp_path / "repo", git_env)
            proj = await app1.state.services.workspace.add_local(str(repo_path))
            project_id = proj.id

            # Save a secret in the shared keystore
            harness.keystore.set("test_key", "secret_value")

            # Verify client access
            resp = await client1.get(f"/api/projects/{project_id}")
            assert resp.status_code == 200

            # Setup models
            await harness.setup_models(app1.state.services)
            view = await app1.state.services.agent_settings.get("data-quality", project_id)
            assert view.effective_model == "fake:fake-data-quality"

        # First app lifespan exited. Now start second app with SAME data dir and keystore.
        async with harness.started_app() as app2:
            client2 = harness.client(app2)
            assert harness.keystore.get("test_key") == "secret_value"

            # Project state in sqlite databases survives across app restart
            resp2 = await client2.get(f"/api/projects/{project_id}")
            assert resp2.status_code == 200
            assert resp2.json()["id"] == project_id


@pytest.mark.asyncio
async def test_harness_run_due_and_wait_for_agent_idle(
    tmp_path: Path, git_env: dict[str, str]
) -> None:
    """run_due drives scheduler and wait_for_agent_idle polls session busy status."""
    home = tmp_path / "harness-home"
    async with create_e2e_harness(home) as harness, harness.started_app() as app:
        client = harness.client(app)
        project_id = await add_local_project(app, tmp_path, git_env)

        # Test run_due with (services, now)
        now = harness.clock.now()
        await run_due(app.state.services, now)

        # Test run_due with (services, project_id, now)
        await run_due(app.state.services, project_id, now)

        # wait_for_agent_idle on onboarding agent (starts idle)
        session = await wait_for_agent_idle(client, project_id, "onboarding", timeout_s=5.0)
        assert session["busy"] is False


def test_fixture_project_payloads() -> None:
    """Verify fixture_project handoff_payload and playwright_handoff_payload validate against schema."""
    mlflow_url = "http://127.0.0.1:5000"
    payload_dict = handoff_payload(mlflow_url)
    validated = HandoffPayload.model_validate(payload_dict)

    assert len(validated.models) == 1
    model = validated.models[0]
    assert model.name == MODEL_NAME
    assert model.production_signal is not None
    assert model.production_signal.kind == "ci_job"
    assert model.production_signal.ref == SCORING_WORKFLOW

    # Pipeline stages and downstream relationships
    assert is_downstream(TABLE_BOOKINGS, TABLE_PREDICTIONS, validated) is True
    assert is_downstream(TABLE_BOOKINGS, TABLE_WEATHER, validated) is False
    assert is_downstream(TABLE_WEATHER, TABLE_PREDICTIONS, validated) is True

    # Feed
    assert [f.name for f in validated.operations.feeds] == [WEATHER_FEED_NAME, TEAM_FEED_NAME]

    # On-call
    assert len(validated.operations.on_call) == 2
    assert validated.operations.on_call[0].contacts[0].channel == "discord"
    assert validated.operations.on_call[0].escalation_order == 1
    assert validated.operations.on_call[1].escalation_order == 2

    # Checks
    check_ids = [c.check_id for c in validated.operations.checks]
    assert TEAM_CHECK_ID in check_ids
    assert AGENT_CHECK_ID in check_ids

    # Playwright variant
    playwright_dict = playwright_handoff_payload(mlflow_url)
    playwright_val = HandoffPayload.model_validate(playwright_dict)
    assert len(playwright_val.operations.feeds) == 0
    assert len(playwright_val.operations.checks) == 0
    assert len(playwright_val.tracking) == 1


def test_fixture_project_rules_and_helpers(tmp_path: Path, git_env: dict[str, str]) -> None:
    """Verify rollback_rule_spec, write_results_record, and create_fixture_repo."""
    rule = rollback_rule_spec()
    assert rule.hook_id == ROLLBACK_HOOK_ID
    assert rule.hook_kind == "rollback"
    assert len(rule.terms) == 1
    assert rule.terms[0].check_id == f"ci:{SCORING_WORKFLOW}"
    assert isinstance(rule.terms[0].when, Compare)
    assert rule.terms[0].when.field == "fired"

    cmd = rollback_command_spec()
    assert cmd.kind == "rollback"
    assert cmd.entry_files == ["rollback.py"]

    # monitoring_settings_payload
    settings = monitoring_settings_payload()
    assert settings["second_deadline_minutes"] == SECOND_DEADLINE_MINUTES
    assert settings["second_deadline_minutes"] > 60  # Extends past morning

    # write_results_record
    folder = tmp_path / "conn"
    rec = {
        "check_id": "test_chk",
        "origin": "agent",
        "ran_at": "2026-10-31T05:00:00Z",
        "fired": True,
        "category": "data_quality",
        "subject": "bookings",
    }
    file_path = write_results_record(folder, rec, "results.jsonl")
    assert file_path.exists()
    lines = file_path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["check_id"] == "test_chk"

    # create_fixture_repo
    repo = create_fixture_repo(tmp_path / "repo", git_env)
    assert (repo / "rollback.py").exists()
    assert (repo / "ingest_bookings.py").exists()

    # write_fixture_data_folder
    data_dir = tmp_path / "data"
    write_fixture_data_folder(data_dir)
    assert (data_dir / "bookings.csv").exists()
    assert (data_dir / "weather.csv").exists()


def test_monitoring_script_alert_prioritization() -> None:
    """Alert Prioritization calls MonitoringQuery on wake-up, then concludes."""
    tools = [{"type": "function", "function": {"name": MONITORING_QUERY}}]
    # Turn 1: Wake-up message received
    messages = [
        {"role": "user", "content": "Incident inc_123 changed (opened). Review prioritization."}
    ]
    reply_text, tool_calls = alert_prioritization_reply(messages, tools)
    assert reply_text == ""
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == MONITORING_QUERY
    assert tool_calls[0]["arguments"]["id"] == "inc_123"

    # Turn 2: Tool result received
    messages.append({"role": "tool", "content": json.dumps({"rows": [], "total": 0})})
    reply_text, tool_calls = alert_prioritization_reply(messages, tools)
    assert "complete" in reply_text.lower()
    assert tool_calls is None


def test_monitoring_script_data_quality() -> None:
    """Data Quality calls IncidentUpdate with cause on wake-up, then concludes."""
    tools = [{"type": "function", "function": {"name": INCIDENT_UPDATE}}]
    # Turn 1: Wake-up message received
    messages = [{"role": "user", "content": "Incident inc_dq456 requires diagnosis."}]
    reply_text, tool_calls = data_quality_reply(messages, tools)
    assert reply_text == ""
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == INCIDENT_UPDATE
    assert tool_calls[0]["arguments"]["incident_id"] == "inc_dq456"
    assert tool_calls[0]["arguments"]["cause"] == SCRIPTED_DATA_QUALITY_CAUSE

    # Turn 2: Tool result received
    messages.append({"role": "tool", "content": "Incident inc_dq456 updated."})
    reply_text, tool_calls = data_quality_reply(messages, tools)
    assert "complete" in reply_text.lower()
    assert tool_calls is None


def test_monitoring_script_deployment_rollback() -> None:
    """Deployment & Rollback calls RunHook with rollback hook and rule id."""
    tools = [{"type": "function", "function": {"name": RUN_HOOK}}]
    # Turn 1: Wake-up message received
    messages = [{"role": "user", "content": "Incident inc_rb789 requires diagnosis."}]
    reply_text, tool_calls = deployment_rollback_reply(messages, tools)
    assert reply_text == ""
    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == RUN_HOOK
    assert tool_calls[0]["arguments"]["hook_id"] == ROLLBACK_HOOK_ID
    assert tool_calls[0]["arguments"]["rule_id"] == ROLLBACK_RULE_ID
    assert tool_calls[0]["arguments"]["incident_id"] == "inc_rb789"

    # Turn 2: Tool result received
    messages.append({"role": "tool", "content": "Hook executed successfully."})
    reply_text, tool_calls = deployment_rollback_reply(messages, tools)
    assert "rollback" in reply_text.lower()
    assert tool_calls is None


def test_monitoring_script_onboarding_night() -> None:
    """onboarding_night_reply asks for data, proposes connections, proposes handoff, and finishes."""
    tools = [{"type": "function", "function": {"name": PROPOSE_TOOL_NAME}}]

    # Turn 1: No data path yet
    text, calls = onboarding_night_reply([{"role": "user", "content": "Hello"}], tools)
    assert text == ASK_FOR_DATA
    assert calls is None

    # Turn 2: User provides data path and mlflow url -> propose connections
    user_msg = {
        "role": "user",
        "content": f"{DATA_PATH_KEY}/data/bookings\n{MLFLOW_URL_KEY}http://mlflow",
    }
    text, calls = onboarding_night_reply([user_msg], tools)
    assert text == ""
    assert calls is not None
    assert calls[0]["name"] == PROPOSE_TOOL_NAME
    assert calls[0]["arguments"]["kind"] == "connections"

    # Turn 3: Tool response for connections -> propose handoff
    text, calls = onboarding_night_reply(
        [user_msg, {"role": "tool", "content": "Connections proposed"}], tools
    )
    assert text == ""
    assert calls is not None
    assert calls[0]["name"] == PROPOSE_TOOL_NAME
    assert calls[0]["arguments"]["kind"] == "handoff"

    # Turn 4: Tool response for handoff -> onboarding complete
    text, calls = onboarding_night_reply(
        [
            user_msg,
            {"role": "tool", "content": "Connections proposed"},
            {"role": "tool", "content": "Handoff proposed"},
        ],
        tools,
    )
    assert text == ONBOARDING_COMPLETE
    assert calls is None


def test_fake_openai_models_and_dispatch() -> None:
    """Verify MODELS dictionary and _reply dispatch for new monitoring models in fake_openai."""
    for model_name in (
        "fake-alert-prioritization",
        "fake-data-quality",
        "fake-deployment-rollback",
        "fake-onboarding-night",
    ):
        assert model_name in MODELS

    state = FakeState()
    tools = [{"type": "function", "function": {"name": MONITORING_QUERY}}]

    # Dispatch to alert prioritization
    _text, calls = _reply(
        state,
        "fake-alert-prioritization",
        [{"role": "user", "content": "Incident inc_1 changed"}],
        tools,
    )
    assert calls is not None
    assert calls[0]["name"] == MONITORING_QUERY

    # Dispatch to data quality
    dq_tools = [{"type": "function", "function": {"name": INCIDENT_UPDATE}}]
    _text, calls = _reply(
        state,
        "fake-data-quality",
        [{"role": "user", "content": "Incident inc_2 requires diagnosis"}],
        dq_tools,
    )
    assert calls is not None
    assert calls[0]["name"] == INCIDENT_UPDATE

    # Dispatch to deployment rollback
    rb_tools = [{"type": "function", "function": {"name": RUN_HOOK}}]
    _text, calls = _reply(
        state,
        "fake-deployment-rollback",
        [{"role": "user", "content": "Incident inc_3 scoring failure"}],
        rb_tools,
    )
    assert calls is not None
    assert calls[0]["name"] == RUN_HOOK
