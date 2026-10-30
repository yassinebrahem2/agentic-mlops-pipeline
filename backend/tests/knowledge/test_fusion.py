from __future__ import annotations

from mlops_agents.knowledge.fusion import fuse
from mlops_agents.knowledge.limits import RRF_K


def test_worked_example_order() -> None:
    # Plan worked example: for [[a, b, c], [c, a]] the order is a, c, b
    rankings = [["a", "b", "c"], ["c", "a"]]
    assert fuse(rankings) == ["a", "c", "b"]


def test_single_ranking_returns_itself() -> None:
    rankings = [["kno_1", "kno_2", "kno_3"]]
    assert fuse(rankings) == ["kno_1", "kno_2", "kno_3"]


def test_empty_rankings() -> None:
    assert fuse([]) == []
    assert fuse([[]]) == []
    assert fuse([[], []]) == []


def test_id_in_one_ranking_only_still_appears() -> None:
    rankings = [["kno_1", "kno_2"], ["kno_2", "kno_3"]]
    result = fuse(rankings)
    assert set(result) == {"kno_1", "kno_2", "kno_3"}
    # kno_2 appears in both rankings, so it must rank first
    assert result[0] == "kno_2"


def test_ties_broken_by_position_in_first_ranking_then_by_id() -> None:
    # With k=1:
    # item 'x' at rank 1 in list 0 -> score = 1/(1+1) = 0.5, pos in first ranking = 0
    # item 'y' at rank 3 in list 0 and rank 3 in list 1 -> score = 1/(1+3) + 1/(1+3) = 0.5, pos in first ranking = 2
    # 'x' has earlier pos (0 < 2) -> 'x' comes before 'y'
    rankings = [
        ["x", "other1", "y"],
        ["other2", "other3", "y"],
    ]
    # Note: 'x' score = 1/2 = 0.5; 'y' score = 1/4 + 1/4 = 0.5
    # others have lower scores: other1 = 1/3, other2 = 1/2 (pos 0 in ranking 1), other3 = 1/3
    res = fuse(rankings, k=1)
    # Between x and y (both score 0.5), x has pos 0 in list 0, y has pos 2 in list 0
    assert res.index("x") < res.index("y")

    # Tied score and tied position (both pos 0 in their respective first lists) -> broken by ID
    r_ties = [["b"], ["a"]]
    # b has score 1/(k+1), pos 0 in ranking 0
    # a has score 1/(k+1), pos 0 in ranking 1
    # Both pos 0, tie broken by id: 'a' < 'b'
    assert fuse(r_ties) == ["a", "b"]

    r_ties2 = [["z"], ["m"]]
    assert fuse(r_ties2) == ["m", "z"]


def test_custom_k() -> None:
    # Custom k changes relative weight of rank 1 vs rank 2
    rankings = [["a", "b"], ["b"]]
    res_default = fuse(rankings, k=RRF_K)
    res_k1 = fuse(rankings, k=1)
    assert res_default[0] == "b"
    assert res_k1[0] == "b"


def test_duplicates_within_ranking_ignored() -> None:
    rankings = [["a", "a", "b"], ["b", "a"]]
    assert fuse(rankings) == ["a", "b"]
