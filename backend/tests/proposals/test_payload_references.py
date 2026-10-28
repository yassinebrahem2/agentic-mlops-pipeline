"""HandoffPayload references that resolve inside the payload, and the names that are unique."""

import pytest
from pydantic import ValidationError

from mlops_agents.proposals.payloads import HandoffPayload
from tests.proposals.samples import error_text, handoff, model


def test_table_use_must_match_a_data_role() -> None:
    bad = model(predictions={"connection": "hotel", "table": "nope"})
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(handoff(models=[bad]))
    assert "nope" in error_text(err)
    with pytest.raises(ValidationError) as err2:
        HandoffPayload.model_validate(
            handoff(models=[model(labels={"connection": "other", "table": "labels"})])
        )
    assert "other" in error_text(err2)
    HandoffPayload.model_validate(handoff(models=[model(predictions=None, labels=None)]))


def test_edges_must_join_known_stages() -> None:
    bad = handoff(edges=[{"from_stage": "prepare", "to_stage": "ship"}])
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(bad)
    assert "ship" in error_text(err)
    bad_from = handoff(edges=[{"from_stage": "ghost", "to_stage": "train"}])
    with pytest.raises(ValidationError) as err2:
        HandoffPayload.model_validate(bad_from)
    assert "ghost" in error_text(err2)


def test_stage_names_are_unique() -> None:
    stages = handoff()["pipeline"]
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(handoff(pipeline=[*stages, {**stages[0]}]))
    assert "prepare" in error_text(err)


def test_an_edge_never_joins_a_stage_to_itself() -> None:
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(handoff(edges=[{"from_stage": "train", "to_stage": "train"}]))
    assert "train" in error_text(err)


def test_an_edge_appears_once() -> None:
    edge = {"from_stage": "prepare", "to_stage": "train"}
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(handoff(edges=[edge, {**edge}]))
    assert "prepare" in error_text(err)
    assert "train" in error_text(err)


def test_a_cycle_is_allowed() -> None:
    # Real pipelines feed predictions back into training.
    HandoffPayload.model_validate(
        handoff(
            edges=[
                {"from_stage": "prepare", "to_stage": "train"},
                {"from_stage": "train", "to_stage": "prepare"},
            ]
        )
    )


def test_tracker_alias_signal_must_be_an_environment_alias() -> None:
    bad = model(production_signal={"kind": "tracker_alias", "ref": "champion"})
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(handoff(models=[bad]))
    assert "champion" in error_text(err)
    for kind in ("ci_job", "file"):
        other = model(production_signal={"kind": kind, "ref": "anything"})
        HandoffPayload.model_validate(handoff(models=[other]))
    HandoffPayload.model_validate(handoff(models=[model(production_signal=None)]))


def test_model_names_are_unique() -> None:
    with pytest.raises(ValidationError) as err:
        HandoffPayload.model_validate(handoff(models=[model("dup"), model("dup")]))
    assert "dup" in error_text(err)
