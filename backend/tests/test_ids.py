import re

import pytest

from mlops_agents.clock import now_iso
from mlops_agents.ids import new_id


def test_new_id_shape() -> None:
    value = new_id("prj")
    assert re.fullmatch(r"prj_[0-9A-HJKMNP-TV-Z]{26}", value)


@pytest.mark.parametrize("prefix", ["tsk", "apr", "evt", "aud", "xch", "wak"])
def test_stage_2_prefixes_are_registered(prefix: str) -> None:
    assert re.fullmatch(prefix + r"_[0-9A-HJKMNP-TV-Z]{26}", new_id(prefix))


@pytest.mark.parametrize("prefix", ["con", "trk", "prp", "kno"])
def test_stage_3_prefixes_are_registered(prefix: str) -> None:
    assert re.fullmatch(prefix + r"_[0-9A-HJKMNP-TV-Z]{26}", new_id(prefix))


def test_unknown_prefix_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown id prefix"):
        new_id("xyz")


def test_now_iso_is_utc() -> None:
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", now_iso())
