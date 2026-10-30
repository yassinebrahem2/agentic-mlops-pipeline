"""The kickoff message: marker, summary with the agent's own sections first, brief; the cap."""

from __future__ import annotations

import logging

import pytest

from mlops_agents.agents.catalog import AgentCatalog
from mlops_agents.onboarding.briefs import (
    AUDIT_BRIEFS,
    BRIEF_SECTIONS,
    CUT_LINE,
    KICKOFF_AGENTS,
    KICKOFF_MESSAGE_CAP,
)
from mlops_agents.onboarding.handoff_preview import sections
from mlops_agents.onboarding.kickoff import kickoff_text
from tests.conftest import STAGE_2_TOOLS, STAGE_3_HOOKS
from tests.onboarding.kickoff_env import PROPOSAL, marker, sample_payload
from tests.proposals.samples import model

# The message


def test_the_text_is_the_marker_then_the_summary_then_the_brief() -> None:
    payload = sample_payload()
    for agent in KICKOFF_AGENTS:
        text = kickoff_text(agent, PROPOSAL, payload)
        assert text.splitlines()[0] == marker(PROPOSAL)
        for section in sections(payload).values():
            assert section in text
        assert text.endswith(AUDIT_BRIEFS[agent])
        assert CUT_LINE not in text
        assert len(text) <= KICKOFF_MESSAGE_CAP


def test_a_summary_past_the_cap_is_cut_at_a_section_boundary_keeping_the_agents_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The overflow is in a part no agent owns, so every agent's own sections still fit.
    gaps = [{"what": f"Gap number {i} " + "x" * 80, "next_step": "Fix it"} for i in range(400)]
    payload = sample_payload(gaps=gaps)
    parts = sections(payload)
    assert len(parts["gaps"]) > KICKOFF_MESSAGE_CAP

    for agent in KICKOFF_AGENTS:
        with caplog.at_level(logging.INFO, logger="mlops_agents.onboarding.kickoff"):
            text = kickoff_text(agent, PROPOSAL, payload)
        assert len(text) <= KICKOFF_MESSAGE_CAP
        for key in BRIEF_SECTIONS[agent]:
            assert parts[key] in text
        assert parts["gaps"] not in text
        summary, brief = text.split(CUT_LINE)
        assert brief.strip() == AUDIT_BRIEFS[agent]
        # Cut at a boundary: whole sections, the agent's own first, then the rest in order up
        # to the first one that does not fit.
        own = list(BRIEF_SECTIONS[agent])
        rest = [key for key in parts if key not in own]
        kept_keys = own + rest[: rest.index("gaps")]
        kept = summary.removeprefix(marker(PROPOSAL)).strip()
        assert kept == "\n\n".join(parts[key] for key in kept_keys)
    assert any("cut" in r.getMessage() for r in caplog.records)


def test_the_cap_is_hard_even_when_an_agents_own_section_does_not_fit() -> None:
    payload = sample_payload(models=[model(f"model_{i}") for i in range(60)])
    parts = sections(payload)
    assert len(parts["models"]) > KICKOFF_MESSAGE_CAP

    text = kickoff_text("model-performance", PROPOSAL, payload)
    assert len(text) <= KICKOFF_MESSAGE_CAP
    assert text == "\n\n".join([marker(PROPOSAL), CUT_LINE, AUDIT_BRIEFS["model-performance"]])

    # Data Quality's own sections fit; the cut stops at the first section that does not.
    text = kickoff_text("data-quality", PROPOSAL, payload)
    assert len(text) <= KICKOFF_MESSAGE_CAP
    summary = text.split(CUT_LINE)[0].removeprefix(marker(PROPOSAL)).strip()
    assert summary == "\n\n".join([parts["data_roles"], parts["operations"]])


def test_own_sections_lead_the_summary() -> None:
    payload = sample_payload()
    text = kickoff_text("data-quality", PROPOSAL, payload)
    parts = sections(payload)
    assert text.index(parts["data_roles"]) < text.index(parts["models"])
    assert text.index(parts["operations"]) < text.index(parts["models"])


# Briefs and agents


def test_briefs_cover_exactly_the_kickoff_agents_and_each_can_chat() -> None:
    assert set(AUDIT_BRIEFS) == set(KICKOFF_AGENTS)
    assert set(BRIEF_SECTIONS) == set(KICKOFF_AGENTS)
    catalog = AgentCatalog.load(STAGE_2_TOOLS, hook_names=STAGE_3_HOOKS)
    for agent in KICKOFF_AGENTS:
        assert catalog.get(agent).chat is True


@pytest.mark.parametrize(
    ("agent", "terms"),
    [
        ("data-quality", ["data roles", "feeds", "serving_inputs"]),
        ("model-performance", ["profile", "predictions", "labels", "label delay"]),
        ("retraining", ["train", "evaluate", "entry files", "promotion"]),
        (
            "deployment",
            ["deploy", "rollback", "environments", "production signal", "promotion"],
        ),
    ],
)
def test_each_brief_names_its_part_and_how_to_read_the_rest(agent: str, terms: list[str]) -> None:
    brief = AUDIT_BRIEFS[agent].lower()
    for term in terms:
        assert term in brief, term
    assert "ReadHandoff" in AUDIT_BRIEFS[agent]
    assert "re-audit" in brief  # a newer handoff starts a new kickoff
    assert set(BRIEF_SECTIONS[agent]) <= set(sections(sample_payload()))
