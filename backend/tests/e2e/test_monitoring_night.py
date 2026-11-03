"""Simulated night end-to-end monitoring test (Stage 4 §14, Task 1).

Story progression:
1. Scene 1 (Agent check on bookings & downstream predictions):
   - Team check fires on bookings: stored, no incident, no notification;
     incident on bookings later gets baseline link to it.
   - Agent check fires on bookings: one data_quality incident; Data Quality
     agent woken with incident and records cause via IncidentUpdate.
   - Same check fires on predictions (downstream): attaches to the open incident,
     no second notification.
2. Scene 2 (Weather feed silence):
   - weather daily data feed misses deadline plus grace (02:00 UTC + 30m = 02:30 UTC):
     one silence signal in its own incident, high severity (production depends on it).
   - On-call hours are 09:00 to 18:00, so notification is not sent outside hours,
     held for morning report.
3. Scene 3 (Scoring workflow failure & rule-permitted rollback):
   - Scoring workflow's run fails (subtype failed_scoring): its own critical incident.
   - Fake Discord receives the page before Deployment & Rollback's first request.
   - Deployment & Rollback runs RunHook citing the rollback rule: runs rollback
     hook without approval.
   - Timeline contains rule.satisfied and hook.ran.
   - Past escalation timeout without acknowledgment, second on-call person gets
     the escalation page.
4. Scene 4 (Stopping halfway & restart):
   - Close first app's lifespan mid-night.
   - Write records arriving while offline: condition on table holidays that
     clears before restart, condition on rates still firing. Neither table is
     in a stage, so neither attaches to open incidents.
   - Start second app on same data folder with same key store, run missed jobs
     with run_due.
   - Cleared condition resolves while offline, waking no one.
   - Still-firing opens and notifies once.
   - No incident, notification, or wake is duplicated.
5. Scene 5 (Morning report):
   - Morning report at 09:00 UTC to on-call people lists the night's incidents,
     rollback with rule, what waits for a person, and resolved-offline incident.
6. Scene 6 (Verdict and close):
   - Person's verdict recorded through route (/api/projects/{id}/incidents/{id}/verdict).
   - Person's close route succeeds.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from mlops_agents.agents.approvals.store import ApprovalStore
from mlops_agents.agents.runtime.store import SessionStore
from mlops_agents.agents.tools.base import Preview, RiskClass
from mlops_agents.clock import to_iso
from mlops_agents.connections.contract import LOCAL_FOLDER_KIND
from mlops_agents.monitoring.results.contract import ResultQuery, ResultRecord
from mlops_agents.monitoring.settings import set_monitoring_settings
from mlops_agents.notifications.contract import Message
from mlops_agents.proposals.contract import HANDOFF_KIND, HOOKS_KIND, PARSER_KIND, RULE_KIND
from mlops_agents.storage.tables import agent_wakes, notification_log, sessions, turns
from tests.e2e.fixture_project import (
    AGENT_CHECK_ID,
    CONNECTION_NAME,
    SCORING_WORKFLOW,
    TABLE_BOOKINGS,
    TABLE_PREDICTIONS,
    TEAM_CHECK_ID,
    TEAM_SOURCE_ID,
    WEATHER_FEED_NAME,
    agent_parser_payload,
    create_fixture_repo,
    handoff_payload,
    monitoring_settings_payload,
    rollback_command_spec,
    rollback_rule_spec,
    team_parser_payload,
    write_fixture_data_folder,
    write_results_record,
    write_team_check_record,
)
from tests.e2e.harness import (
    create_e2e_harness,
    run_due,
    wait_for_agent_idle,
)
from tests.fakes.monitoring_script import (
    SCRIPTED_DATA_QUALITY_CAUSE,
    reset_deployment_ids,
    set_deployment_ids,
    set_on_deployment_rollback,
)


@pytest.mark.asyncio
async def test_monitoring_night_end_to_end(tmp_path: Path, git_env: dict[str, str]) -> None:
    """The simulated night end-to-end monitoring story (Scenes 1 through 6)."""
    home = tmp_path / "app-home"
    data_path = tmp_path / "hotel-data"
    repo_path = create_fixture_repo(tmp_path / "repo", git_env)
    write_fixture_data_folder(data_path)

    # Set weather.csv mtime to yesterday so it is not seen as modified inside the night
    weather_old_ts = datetime(2026, 10, 30, 0, 0, 0, tzinfo=UTC).timestamp()
    os.utime(data_path / "weather.csv", (weather_old_ts, weather_old_ts))

    start_time = datetime(2026, 10, 31, 0, 30, 0, tzinfo=UTC)

    try:
        async with create_e2e_harness(home, start_time=start_time) as harness:
            # -----------------------------------------------------------------
            # SETUP IN FIRST APP
            # -----------------------------------------------------------------
            async with harness.started_app() as app1:
                client1 = harness.client(app1)
                services1 = app1.state.services

                proj = await services1.workspace.add_local(str(repo_path))
                project_id = proj.id

                await harness.setup_models(services1)

                # Persist Discord webhook and GitHub token in shared keystore
                discord_url = f"{harness.fake_discord.base_url}/api/webhooks/123/token_secret"
                await services1.integration_settings.set_discord(
                    enabled=True, webhook_url=discord_url
                )
                await services1.github.set_token("gh_fake_night_token")

                # Setup data connection
                await services1.connections.create(
                    project_id,
                    kind=LOCAL_FOLDER_KIND,
                    name=CONNECTION_NAME,
                    settings={"path": str(data_path)},
                )

                # Apply handoff via proposals (creates tracking sources and registers handoff)
                handoff_prop = await services1.proposals.create(
                    project_id,
                    agent="architect",
                    kind=HANDOFF_KIND,
                    key="project",
                    payload=handoff_payload(),
                    turn_id="trn_night_handoff",
                    tool_call_id="call_night_handoff",
                )
                handoff_res = await services1.proposals.decide(
                    project_id, handoff_prop.id, approved=True
                )
                assert handoff_res is not None
                assert handoff_res.failed == {}

                # Propose rollback hook via proposals (HooksProposalCard) and decide
                hook_spec = rollback_command_spec()
                hook_prop = await services1.proposals.create(
                    project_id,
                    agent="architect",
                    kind=HOOKS_KIND,
                    key="project",
                    payload={
                        "hooks": [
                            {
                                "name": hook_spec.kind,
                                "model": None,
                                "spec": hook_spec.model_dump(mode="json"),
                                "why": "Automated rollback for scoring failure",
                            }
                        ]
                    },
                    turn_id="trn_night_hook",
                    tool_call_id="call_night_hook",
                )
                hook_res = await services1.proposals.decide(project_id, hook_prop.id, approved=True)
                assert hook_res is not None
                assert hook_res.failed == {}

                # Find and approve rollback hook via API route to pin commit
                all_hooks = await services1.hooks.list(project_id)
                rollback_hook = next(h for h in all_hooks if h.spec.kind == "rollback")
                approve_resp = await client1.post(
                    f"/api/projects/{project_id}/hooks/{rollback_hook.id}/approve"
                )
                assert approve_resp.status_code == 200, approve_resp.text

                # Propose and store rollback rule via proposals
                rule_spec = rollback_rule_spec(hook_id=rollback_hook.id)
                rule_key_val = (
                    f"{rule_spec.model}:{rule_spec.name}" if rule_spec.model else rule_spec.name
                )
                rule_prop = await services1.proposals.create(
                    project_id,
                    agent="architect",
                    kind=RULE_KIND,
                    key=rule_key_val,
                    payload={"spec": rule_spec.model_dump(mode="json")},
                    turn_id="trn_night_rule",
                    tool_call_id="call_night_rule",
                )
                rule_res = await services1.proposals.decide(project_id, rule_prop.id, approved=True)
                assert rule_res is not None
                assert rule_res.failed == {}
                rule = (await services1.rules.list(project_id))[0]

                # The team check's mapping parser, approved before the night; no agent proposes it
                parser_proposal = await services1.proposals.create(
                    project_id,
                    agent="architect",
                    kind=PARSER_KIND,
                    key=TEAM_SOURCE_ID,
                    payload=team_parser_payload(ran_at=harness.clock.now()),
                    turn_id="trn_night_parser",
                    tool_call_id="call_night_parser",
                )
                parser_result = await services1.proposals.decide(
                    project_id, parser_proposal.id, approved=True
                )
                assert parser_result is not None
                assert parser_result.failed == {}

                # The agent check's mapping parser
                agent_src_id = f"file:{CONNECTION_NAME}:checks/*.jsonl"
                agent_prop = await services1.proposals.create(
                    project_id,
                    agent="architect",
                    kind=PARSER_KIND,
                    key=agent_src_id,
                    payload=agent_parser_payload(ran_at=harness.clock.now()),
                    turn_id="trn_night_agent_parser",
                    tool_call_id="call_night_agent_parser",
                )
                agent_res = await services1.proposals.decide(
                    project_id, agent_prop.id, approved=True
                )
                assert agent_res is not None
                assert agent_res.failed == {}

                # Configure deployment rollback scripted reply IDs and record invocation order
                order: list[str] = []
                set_deployment_ids(hook_id=rollback_hook.id, rule_id=rule.id)
                set_on_deployment_rollback(lambda: order.append("deployment_rollback_request"))

                class TrackingPosts(list[dict[str, Any]]):
                    def append(self, item: dict[str, Any]) -> None:
                        order.append("discord_page")
                        super().append(item)

                harness.fake_discord.state.posts = TrackingPosts(harness.fake_discord.state.posts)

                # Write monitoring settings
                await set_monitoring_settings(
                    services1.databases,
                    project_id,
                    monitoring_settings_payload(),
                )

                # Clear any initial discord posts
                harness.fake_discord.state.reset()

                # Stop background scheduler loop so the test drives execution deterministically via run_due
                await services1.scheduler.stop()

                # -----------------------------------------------------------------
                # SCENE 1: Agent check on bookings & downstream predictions
                # -----------------------------------------------------------------
                # 1a. Team check fires on bookings at 01:00 UTC
                harness.clock.set(datetime(2026, 10, 31, 1, 0, 0, tzinfo=UTC))
                write_team_check_record(
                    data_path / "team",
                    ran_at=harness.clock.now(),
                    table=TABLE_BOOKINGS,
                )
                await run_due(services1, project_id, harness.clock.now())

                # Stored in results, no incident, no notification
                q_res = await services1.results.query(
                    project_id, ResultQuery(check_id=TEAM_CHECK_ID)
                )
                assert q_res.total == 1
                # It came through the approved mapping parser, as the team's own record
                assert q_res.rows[0].source_id == TEAM_SOURCE_ID
                assert q_res.rows[0].origin == "team"
                assert q_res.rows[0].fired is True
                inc_page = await services1.incidents.list(project_id)
                assert inc_page.total == 0
                assert len(harness.fake_discord.state.posts) == 0

                # 1b. Agent check fires on bookings at 01:30 UTC
                harness.clock.set(datetime(2026, 10, 31, 1, 30, 0, tzinfo=UTC))
                rec_agent_bookings = ResultRecord(
                    check_id=AGENT_CHECK_ID,
                    origin="agent",
                    ran_at=harness.clock.now(),
                    fired=True,
                    category="data_quality",
                    subject=TABLE_BOOKINGS,
                    model=None,
                    detail={"null_lead_times": 42},
                    source_ref="checks/checks.jsonl",
                )
                write_results_record(
                    data_path / "checks", rec_agent_bookings.model_dump(mode="json"), "checks.jsonl"
                )

                await run_due(services1, project_id, harness.clock.now())

                # Wait for Data Quality agent turn to complete
                await wait_for_agent_idle(client1, project_id, "data-quality")

                # One data_quality incident opened and cause recorded
                inc_page = await services1.incidents.list(project_id)
                assert inc_page.total == 1
                bookings_inc = inc_page.rows[0]
                assert bookings_inc.category == "data_quality"

                updated_bookings_inc = await services1.incidents.get(project_id, bookings_inc.id)
                assert updated_bookings_inc is not None
                assert updated_bookings_inc.cause == SCRIPTED_DATA_QUALITY_CAUSE

                # Data Quality agent was woken once for bookings incident
                engine1 = await services1.databases.project(project_id)
                async with engine1.connect() as conn:
                    stmt = select(agent_wakes).where(agent_wakes.c.agent == "data-quality")
                    dq_wakes_1b = (await conn.execute(stmt)).mappings().all()
                assert len(dq_wakes_1b) == 1
                assert dq_wakes_1b[0]["subject"] == bookings_inc.id

                # Incident on bookings gets a baseline link to the team check
                links = await services1.baseline.links(project_id, bookings_inc.id)
                assert len(links) == 1
                assert links[0].team_check_id == TEAM_CHECK_ID

                # 01:30 is outside on-call hours (09:00 - 18:00 UTC), so notification is held for morning
                async with engine1.connect() as conn:
                    stmt = select(notification_log).where(
                        notification_log.c.incident == bookings_inc.id
                    )
                    notifs_bookings_1b = (await conn.execute(stmt)).mappings().all()
                assert len(notifs_bookings_1b) == 0
                assert len(harness.fake_discord.state.posts) == 0

                # 1c. Same check fires on predictions (downstream) at 01:40 UTC
                harness.clock.set(datetime(2026, 10, 31, 1, 40, 0, tzinfo=UTC))
                rec_agent_predictions = ResultRecord(
                    check_id=AGENT_CHECK_ID,
                    origin="agent",
                    ran_at=harness.clock.now(),
                    fired=True,
                    category="data_quality",
                    subject=TABLE_PREDICTIONS,
                    model=None,
                    detail={"null_predictions": 15},
                    source_ref="checks/checks.jsonl",
                )
                write_results_record(
                    data_path / "checks",
                    rec_agent_predictions.model_dump(mode="json"),
                    "checks.jsonl",
                )

                posts_before_predictions = len(harness.fake_discord.state.posts)
                await run_due(services1, project_id, harness.clock.now())

                # Attaches to the open incident, no second incident, no second notification
                inc_page = await services1.incidents.list(project_id)
                assert inc_page.total == 1
                sigs = await services1.incidents.signals(project_id, bookings_inc.id)
                assert len(sigs) == 2
                assert {s.subject for s in sigs} == {TABLE_BOOKINGS, TABLE_PREDICTIONS}
                assert len(harness.fake_discord.state.posts) == posts_before_predictions

                # notification_log count for bookings incident remains 0 after downstream predictions
                async with engine1.connect() as conn:
                    stmt = select(notification_log).where(
                        notification_log.c.incident == bookings_inc.id
                    )
                    notifs_bookings_1c = (await conn.execute(stmt)).mappings().all()
                assert len(notifs_bookings_1c) == len(notifs_bookings_1b) == 0

                # Data Quality was not woken again for downstream predictions
                async with engine1.connect() as conn:
                    stmt = select(agent_wakes).where(agent_wakes.c.agent == "data-quality")
                    dq_wakes_1c = (await conn.execute(stmt)).mappings().all()
                assert len(dq_wakes_1c) == 1

                # -----------------------------------------------------------------
                # SCENE 2: Weather feed silence at 02:30 UTC (past grace)
                # -----------------------------------------------------------------
                # weather daily feed misses deadline (02:00) + grace (30m) = 02:30 UTC
                harness.clock.set(datetime(2026, 10, 31, 2, 31, 0, tzinfo=UTC))
                posts_before_weather = len(harness.fake_discord.state.posts)

                await run_due(services1, project_id, harness.clock.now())

                inc_page = await services1.incidents.list(project_id)
                assert inc_page.total == 2
                weather_inc = next(i for i in inc_page.rows if i.id != bookings_inc.id)
                assert weather_inc.severity == "high"
                assert weather_inc.subtype == "silence"

                weather_sigs = await services1.incidents.signals(project_id, weather_inc.id)
                assert len(weather_sigs) == 1
                assert weather_sigs[0].kind == "silence"
                assert weather_sigs[0].subject == WEATHER_FEED_NAME

                # On-call hours are 09:00 - 18:00; notification held for morning report
                assert len(harness.fake_discord.state.posts) == posts_before_weather

                # -----------------------------------------------------------------
                # SCENE 3: Scoring workflow failure & rule-permitted rollback
                # -----------------------------------------------------------------
                # 3a. Scoring workflow run fails at 03:00 UTC
                harness.clock.set(datetime(2026, 10, 31, 3, 0, 0, tzinfo=UTC))
                failed_run: dict[str, Any] = {
                    "id": 101,
                    "name": "Scoring",
                    "path": f".github/workflows/{SCORING_WORKFLOW}",
                    "status": "completed",
                    "conclusion": "failure",
                    "run_started_at": harness.clock.now().isoformat(),
                    "created_at": harness.clock.now().isoformat(),
                    "updated_at": harness.clock.now().isoformat(),
                    "head_sha": "abc123sha",
                    "html_url": "https://github.com/octo/repo/actions/runs/101",
                }
                harness.fake_github.state.workflow_runs["octo/repo"] = [failed_run]

                posts_before_scoring = len(harness.fake_discord.state.posts)

                await run_due(services1, project_id, harness.clock.now())

                # Fake Discord receives page before Deployment & Rollback completes its request
                assert len(harness.fake_discord.state.posts) == posts_before_scoring + 1

                # Wait for Deployment & Rollback agent turn to execute rollback hook
                await wait_for_agent_idle(client1, project_id, "deployment")

                # The page happens first, so the team knows before the agent starts acting
                assert "discord_page" in order
                assert "deployment_rollback_request" in order
                assert order.index("discord_page") < order.index("deployment_rollback_request")

                # Scoring incident check: critical severity, subtype failed_scoring
                inc_page = await services1.incidents.list(project_id)
                scoring_inc = next(i for i in inc_page.rows if i.subtype == "failed_scoring")
                assert scoring_inc.severity == "critical"

                # Timeline contains rule.satisfied and hook.ran
                evs = await services1.events.page(project_id, subject_id=scoring_inc.id, limit=50)
                ev_types = [e.type for e in evs]
                assert "rule.satisfied" in ev_types
                assert "hook.ran" in ev_types

                # Rollback hook ran with basis == "rule" and ok == True, executed rollback.py, no approvals pending
                runs = await services1.hooks.runs(project_id, kinds=["rollback"])
                assert len(runs) == 1
                assert runs[0].basis == "rule"
                assert runs[0].ok is True
                assert runs[0].rule_id == rule.id
                assert any("rollback.py" in " ".join(call) for call in harness.docker_runner.calls)
                assert await services1.approvals.pending(project_id) == []

                # 3b. Past escalation timeout without acknowledgment (30 min timeout -> advance 31 min)
                harness.clock.advance(timedelta(minutes=31))
                await run_due(services1, project_id, harness.clock.now())

                # Second on-call person (Bob) gets the escalation page
                assert len(harness.fake_discord.state.posts) >= posts_before_scoring + 2

                async with engine1.connect() as conn:
                    stmt = select(notification_log).where(notification_log.c.kind == "escalation")
                    escalation_rows = (await conn.execute(stmt)).mappings().all()
                    assert len(escalation_rows) >= 1
                    assert escalation_rows[0]["person"].lower() == "bob"

            # -----------------------------------------------------------------
            # SCENE 4: Stopping halfway & restart
            # -----------------------------------------------------------------
            # Write records arriving while offline:
            # - table 'holidays' condition fires and then clears before restart
            # - table 'rates' condition fires and is still firing
            t_off_1 = harness.clock.now() + timedelta(minutes=5)
            t_off_2 = harness.clock.now() + timedelta(minutes=10)
            t_off_3 = harness.clock.now() + timedelta(minutes=15)

            rec_holidays_fire = ResultRecord(
                check_id="chk_holidays",
                origin="agent",
                ran_at=t_off_1,
                fired=True,
                category="data_quality",
                subject="holidays",
                model=None,
                detail={"missing": 10},
                source_ref="checks/checks.jsonl",
            )
            rec_holidays_clear = ResultRecord(
                check_id="chk_holidays",
                origin="agent",
                ran_at=t_off_2,
                fired=False,
                category="data_quality",
                subject="holidays",
                model=None,
                detail={"missing": 0},
                source_ref="checks/checks.jsonl",
            )
            rec_rates_fire = ResultRecord(
                check_id="chk_rates",
                origin="agent",
                ran_at=t_off_3,
                fired=True,
                category="data_quality",
                subject="rates",
                model=None,
                detail={"outliers": 7},
                source_ref="checks/checks.jsonl",
            )

            for rec in (rec_holidays_fire, rec_holidays_clear, rec_rates_fire):
                write_results_record(
                    data_path / "checks", rec.model_dump(mode="json"), "checks.jsonl"
                )

            # Advance clock to restart instant
            harness.clock.set(datetime(2026, 10, 31, 5, 0, 0, tzinfo=UTC))

            # Start second app on same data folder with same key store
            async with harness.started_app() as app2:
                client2 = harness.client(app2)
                services2 = app2.state.services

                await services2.scheduler.stop()

                # Run missed jobs with run_due
                await run_due(services2, project_id, harness.clock.now())

                # Wait for data-quality to finish investigating rates
                await wait_for_agent_idle(client2, project_id, "data-quality")

                # Cleared condition (holidays) resolves while offline, waking no one
                inc_page2 = await services2.incidents.list(project_id, limit=20)
                assert inc_page2.total == 5
                holidays_inc = next(
                    i
                    for i in inc_page2.rows
                    if i.cause_key.startswith(f"{len('holidays')}:holidays")
                )
                assert holidays_inc.resolved_offline is True
                assert holidays_inc.status == "resolved"

                # Still-firing (rates) opens and notifies once
                rates_inc = next(
                    i for i in inc_page2.rows if i.cause_key.startswith(f"{len('rates')}:rates")
                )
                assert rates_inc.resolved_offline is False
                assert rates_inc.status == "open"

                # Check restart didn't re-run checks already done (exact counts in results store)
                res_holidays = await services2.results.query(
                    project_id, ResultQuery(check_id="chk_holidays")
                )
                assert res_holidays.total == 2
                res_rates = await services2.results.query(
                    project_id, ResultQuery(check_id="chk_rates")
                )
                assert res_rates.total == 1

                # Check holidays woke no one (including alert-prioritization) and rates woke Data Quality once
                engine2 = await services2.databases.project(project_id)
                async with engine2.connect() as conn:
                    stmt_wakes = select(agent_wakes)
                    wakes_rows = (await conn.execute(stmt_wakes)).mappings().all()

                holidays_wakes = [r for r in wakes_rows if r["subject"] == holidays_inc.id]
                assert len(holidays_wakes) == 0

                rates_wakes = [
                    r
                    for r in wakes_rows
                    if r["subject"] == rates_inc.id and r["agent"] == "data-quality"
                ]
                assert len(rates_wakes) == 1

                # Count wakes from fake model's requests: rates woke DQ once (2 model steps: tool + conclusion), holidays woke no one
                rates_dq_requests = [
                    r
                    for r in harness.fake_openai.state.requests
                    if rates_inc.id in str(r) and r.get("model") == "fake-data-quality"
                ]
                assert len(rates_dq_requests) == 2
                holidays_requests = [
                    r for r in harness.fake_openai.state.requests if holidays_inc.id in str(r)
                ]
                assert len(holidays_requests) == 0

                # Check rates notification was held for morning
                async with engine2.connect() as conn:
                    stmt_rates_notifs = select(notification_log).where(
                        notification_log.c.incident == rates_inc.id
                    )
                    rates_notifs = (await conn.execute(stmt_rates_notifs)).mappings().all()
                assert len(rates_notifs) == 0

                # Check no duplicate notifications in notification_log
                async with engine2.connect() as conn:
                    stmt_notifs = select(notification_log)
                    notif_rows = (await conn.execute(stmt_notifs)).mappings().all()
                    notif_keys = [(r["incident"], r["person"], r["kind"]) for r in notif_rows]
                    assert len(notif_keys) == len(set(notif_keys))

                # Check no duplicate notification.sent events per (incident, person) after restart
                notif_events = await services2.events.page(
                    project_id, types=["notification.sent"], limit=100
                )
                sent_pairs = [
                    (e.subject_id, str(e.payload.get("person")))
                    for e in notif_events
                    if e.subject_id
                ]
                assert len(sent_pairs) == len(set(sent_pairs))

                # -----------------------------------------------------------------
                # SCENE 5: Morning report at 09:00 UTC
                # -----------------------------------------------------------------
                # Create a pending approval waiting for a person at morning report
                async with engine2.connect() as conn:
                    dq_sess_row = (
                        (
                            await conn.execute(
                                select(sessions).where(sessions.c.agent == "data-quality")
                            )
                        )
                        .mappings()
                        .one()
                    )
                    dq_session_id = str(dq_sess_row["id"])

                async with engine2.begin() as conn:
                    await conn.execute(
                        turns.insert().values(
                            id="trn_dq_followup",
                            session_id=dq_session_id,
                            trigger_message_id="msg_trigger_followup",
                            status="waiting",
                            error=None,
                            created_at=to_iso(harness.clock.now()),
                            started_at=to_iso(harness.clock.now()),
                            ended_at=None,
                        )
                    )

                approval_msg = await SessionStore(services2.databases).add_message(
                    project_id,
                    dq_session_id,
                    turn_id="trn_dq_followup",
                    author_kind="agent",
                    author_name="data-quality",
                    parts=[],
                    status="waiting",
                )

                await ApprovalStore(services2.databases).create(
                    project_id,
                    id="apr_dq_review",
                    turn_id="trn_dq_followup",
                    session_id=dq_session_id,
                    message_id=approval_msg.id,
                    agent="data-quality",
                    tool_call_id="call_dq_review",
                    tool="review_action",
                    risk_class=RiskClass.WRITE_CODE,
                    preview=Preview(
                        kind="text",
                        title="Data quality follow-up",
                        body="Manual review of bookings data",
                    ),
                    proposal_kind=None,
                )

                harness.clock.set(datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC))
                posts_before_morning = len(harness.fake_discord.state.posts)

                morning_deliveries: dict[str, Message] = {}
                orig_send = services2.notification_channels["discord"].send

                async def tracking_send(target: str, message: Message) -> None:
                    morning_deliveries[target] = message
                    await orig_send(target, message)

                services2.notification_channels["discord"].send = tracking_send

                await run_due(services2, project_id, harness.clock.now())

                # Assert one report each for u_alice and u_bob by recipient
                assert set(morning_deliveries.keys()) == {"u_alice", "u_bob"}
                morning_posts = harness.fake_discord.state.posts[posts_before_morning:]
                assert len(morning_posts) == 2

                # Check contents of morning report
                report_text = " ".join(str(p) for p in morning_posts)
                assert "Morning report" in report_text
                assert "resolved offline" in report_text.lower()

                # Lists all 5 night incidents by ID
                for inc in (bookings_inc, weather_inc, scoring_inc, holidays_inc, rates_inc):
                    assert inc.id in report_text

                # Mentions the rule cited in the rollback
                assert "rollback (rule)" in report_text.lower()

                # Assert rendered Waiting: section is present with what waits
                assert "Waiting:" in report_text
                assert "data-quality: review_action" in report_text

                # -----------------------------------------------------------------
                # SCENE 6: Verdict and close
                # -----------------------------------------------------------------
                # Person decides the pending approval
                pending_apps = await services2.approvals.pending(project_id)
                assert len(pending_apps) == 1
                await services2.approvals.decide(
                    project_id, pending_apps[0].id, approve=True, note="approved"
                )
                assert await services2.approvals.pending(project_id) == []

                # Person records verdict through route
                verdict_resp = await client2.post(
                    f"/api/projects/{project_id}/incidents/{bookings_inc.id}/verdict",
                    json={"value": "real_issue"},
                )
                assert verdict_resp.status_code == 200, verdict_resp.text
                assert verdict_resp.json()["value"] == "real_issue"

                # Person closes incident through route
                close_resp = await client2.post(
                    f"/api/projects/{project_id}/incidents/{bookings_inc.id}/close"
                )
                assert close_resp.status_code == 200, close_resp.text
                assert close_resp.json()["status"] == "closed"
    finally:
        reset_deployment_ids()
