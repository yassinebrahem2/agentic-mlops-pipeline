import re

import pytest

from mlops_agents.clock import now_iso
from mlops_agents.ids import new_id


def test_new_id_shape() -> None:
    value = new_id("prj")
    assert re.fullmatch(r"prj_[0-9A-HJKMNP-TV-Z]{26}", value)


def test_unknown_prefix_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown id prefix"):
        new_id("xyz")


def test_now_iso_is_utc() -> None:
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", now_iso())
