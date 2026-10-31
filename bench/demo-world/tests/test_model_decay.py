from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from demo_world import answer_key, backfill, model_decay, paths, scenarios, simulate, source
from demo_world.answer_key import Record
from tests.fixture import FIXTURE_CSV

# A world first played on ORIGIN, with a decay window of arrival dates well after it: the share
# ramps up over March, then holds through April.
ORIGIN = date(2016, 1, 1)
START, RAMP_END, END = date(2016, 3, 1), date(2016, 3, 31), date(2016, 4, 30)
LAST_ARRIVAL = date(2016, 5, 31)
GROUP = (("market_segment", "Online TA"), ("deposit_type", "No Deposit"))
ENTRY = (
    '[[injection]]\nkind = "model_decay"\n'
    'group = { market_segment = "Online TA", deposit_type = "No Deposit" }\n'
    f"max_share = 1.0\nstart = {START}\nramp_end = {RAMP_END}\nend = {END}\n"
)


@pytest.fixture(scope="module")
def loaded() -> pd.DataFrame:
    """A few thousand bookings shaped like the source, where long lead times cancel more.

    The fixture CSV has too few rows for a rate or a ramp, so its group booking (id 7) is
    repeated with drawn dates, segments and outcomes. Bookings arriving in the window cancel a
    little more often than earlier ones, so keeping the pre-window rate takes the cut to do it.
    """
    rng = np.random.default_rng(7)
    count = 4000
    first_arrival = pd.Timestamp("2015-11-01")
    frame = source.load_source(FIXTURE_CSV).iloc[[7] * count].reset_index(drop=True)
    arrival = first_arrival + pd.to_timedelta(rng.integers(0, 213, count), unit="D")
    lead = rng.integers(0, 120, count)
    booking = arrival - pd.to_timedelta(lead, unit="D")
    in_window = (arrival >= pd.Timestamp(START)) & (arrival <= pd.Timestamp(END))
    chance = np.where(in_window, 0.15 + 0.6 * lead / 120, 0.1 + 0.4 * lead / 120)
    canceled = rng.random(count) < chance
    cancelled_on = booking + pd.to_timedelta((rng.random(count) * lead).astype(int), unit="D")
    return frame.assign(
        booking_id=np.arange(count, dtype="int64"),
        lead_time=lead.astype("int64"),
        arrival_date=arrival,
        booking_date=booking,
        arrival_month=arrival.month.astype("int8"),
        arrival_week=arrival.isocalendar().week.to_numpy().astype("int8"),
        market_segment=np.where(rng.random(count) < 0.7, "Online TA", "Offline TA/TO"),
        deposit_type=np.where(rng.random(count) < 0.9, "No Deposit", "Non Refund"),
        is_canceled=canceled.astype("int64"),
        label_date=pd.Series(np.where(canceled, cancelled_on, arrival)),
    )


def _rule(max_share: float = 1.0) -> model_decay.Rule:
    return model_decay.Rule(
        group=GROUP, start=START, ramp_end=RAMP_END, end=END, max_share=max_share
    )


def _in_group(frame: pd.DataFrame) -> pd.Series:
    return (frame["market_segment"] == "Online TA") & (frame["deposit_type"] == "No Deposit")


def _scope(frame: pd.DataFrame) -> pd.Series:
    arrival = frame["arrival_date"]
    in_window = (arrival >= pd.Timestamp(START)) & (arrival <= pd.Timestamp(END))
    return _in_group(frame) & in_window & (frame["booking_date"] >= pd.Timestamp(ORIGIN))


def test_it_changes_only_the_labels_of_the_groups_window_bookings_made_in_the_run(
    loaded: pd.DataFrame,
) -> None:
    relabelled = _rule().relabel(loaded, ORIGIN)

    label_columns = ["is_canceled", "label_date"]
    assert relabelled.drop(columns=label_columns).equals(loaded.drop(columns=label_columns))
    changed = (relabelled[label_columns] != loaded[label_columns]).any(axis=1)
    assert changed.sum() > 100
    assert not (changed & ~_scope(loaded)).any()


def test_it_leaves_bookings_made_before_the_world_started_as_they_were(
    loaded: pd.DataFrame,
) -> None:
    # Their bookings, and perhaps their labels, are history the backfill already wrote.
    before = loaded["booking_date"] < pd.Timestamp(ORIGIN)
    arrival = loaded["arrival_date"]
    in_window = (arrival >= pd.Timestamp(START)) & (arrival <= pd.Timestamp(END))
    assert (before & in_window & _in_group(loaded)).sum() > 10

    relabelled = _rule().relabel(loaded, ORIGIN)

    assert relabelled[before].equals(loaded[before])


@pytest.mark.parametrize("max_share", [0.5, 1.0])
def test_it_keeps_the_groups_rate_within_0_02_of_its_pre_window_rate(
    loaded: pd.DataFrame, max_share: float
) -> None:
    pre_window = loaded.loc[
        _in_group(loaded) & (loaded["arrival_date"] < pd.Timestamp(START)), "is_canceled"
    ].mean()
    scope = _scope(loaded)
    # Without the cut doing its work the window's bookings would cancel more.
    assert loaded.loc[scope, "is_canceled"].mean() - pre_window > 0.03

    relabelled = _rule(max_share).relabel(loaded, ORIGIN)

    assert abs(relabelled.loc[scope, "is_canceled"].mean() - pre_window) <= 0.02


def test_its_followers_cancel_below_the_cut_and_not_above(loaded: pd.DataFrame) -> None:
    rule = _rule()
    follows = rule.followers(loaded, ORIGIN)
    cut = rule.cut(loaded, ORIGIN)

    relabelled = rule.relabel(loaded, ORIGIN)

    assert 0 < cut < loaded["lead_time"].max()
    expected = (loaded.loc[follows, "lead_time"] < cut).astype("int64")
    assert relabelled.loc[follows, "is_canceled"].equals(expected)
    # The rule runs against the source, where long lead times cancel more, and changes both
    # sides: a follower at or above the cut that cancelled in the source is kept, and one below
    # it that stayed is cancelled.
    above = follows & (loaded["lead_time"] >= cut)
    below = follows & (loaded["lead_time"] < cut)
    assert relabelled.loc[above, "is_canceled"].sum() == 0
    assert (above & (loaded["is_canceled"] == 1)).sum() > 50
    assert relabelled.loc[below, "is_canceled"].all()
    assert (below & (loaded["is_canceled"] == 0)).sum() > 20


def test_the_share_ramps_from_0_at_the_start_to_max_share_then_holds() -> None:
    rule = _rule(0.6)

    assert rule.share(START) == 0
    assert rule.share(START + (RAMP_END - START) / 2) == pytest.approx(0.3)
    assert rule.share(RAMP_END - timedelta(days=1)) < 0.6
    assert rule.share(RAMP_END) == pytest.approx(0.6)
    assert rule.share(RAMP_END + timedelta(days=10)) == pytest.approx(0.6)
    assert rule.share(END) == pytest.approx(0.6)
    assert rule.share(START - timedelta(days=1)) == 0
    assert rule.share(END + timedelta(days=1)) == 0


def test_the_followers_share_ramps_by_arrival_date(loaded: pd.DataFrame) -> None:
    follows = _rule(0.6).followers(loaded, ORIGIN)
    scope = _scope(loaded)
    arrival = loaded["arrival_date"]

    assert not (follows & ~scope).any()
    assert not follows[arrival == pd.Timestamp(START)].any()
    early = scope & (arrival < pd.Timestamp(START + timedelta(days=8)))
    held = scope & (arrival >= pd.Timestamp(RAMP_END))
    # Expected about 0.6 * 3.5/30 early, and 0.6 over the whole hold.
    assert follows[early].mean() < 0.15
    assert 0.52 < follows[held].mean() < 0.68


def test_a_label_date_moves_only_when_the_outcome_changes(loaded: pd.DataFrame) -> None:
    relabelled = _rule().relabel(loaded, ORIGIN)

    flipped = relabelled["is_canceled"] != loaded["is_canceled"]
    kept = ~flipped
    assert relabelled.loc[kept, "label_date"].equals(loaded.loc[kept, "label_date"])
    cancelled = flipped & (relabelled["is_canceled"] == 1)
    kept_on = flipped & (relabelled["is_canceled"] == 0)
    assert cancelled.sum() > 10
    assert kept_on.sum() > 10
    # A new cancellation is released between its booking and its arrival, never before it was
    # booked; a booking no longer cancelled is released on its arrival, as any stay is.
    new = relabelled[cancelled]
    assert (new["label_date"] >= new["booking_date"]).all()
    assert ((new["label_date"] < new["arrival_date"]) | (new["lead_time"] == 0)).all()
    assert new["label_date"].nunique() > 10
    stays = relabelled[kept_on]
    assert stays["label_date"].equals(stays["arrival_date"])


def test_it_changes_the_same_labels_on_every_run(loaded: pd.DataFrame) -> None:
    assert _rule().relabel(loaded, ORIGIN).equals(_rule().relabel(loaded.copy(), ORIGIN))


def test_a_group_with_no_booking_before_the_window_is_refused(loaded: pd.DataFrame) -> None:
    rule = model_decay.Rule(
        group=(("market_segment", "Online TA"),),
        start=date(2015, 11, 1),
        ramp_end=RAMP_END,
        end=END,
        max_share=1.0,
    )

    with pytest.raises(model_decay.DecayError, match="before 2015-11-01"):
        rule.relabel(loaded, ORIGIN)


def test_the_scenario_file_reads_a_model_decay_entry(tmp_path: Path) -> None:
    path = tmp_path / "scenario.toml"
    path.write_text(ENTRY, encoding="utf-8")

    (problem,) = scenarios.load(path).problems

    assert isinstance(problem, scenarios.ModelDecay)
    assert problem.rule() == _rule()
    assert problem.records() == [
        Record(
            "model_decay",
            "model_performance",
            "market_segment,deposit_type,lead_time",
            "2016-03-01",
            "2016-04-30",
        )
    ]


def _world(loaded: pd.DataFrame, root: Path) -> simulate.World:
    scenario_file = root / "scenario.toml"
    root.mkdir(parents=True, exist_ok=True)
    scenario_file.write_text(ENTRY, encoding="utf-8")
    world = simulate.World(
        loaded=loaded,
        data=root / "data",
        folder=root / "world",
        scenario=scenarios.load(scenario_file),
    )
    backfill.backfill(loaded, world.data, ORIGIN)
    return world


def _play(world: simulate.World, end: date = LAST_ARRIVAL) -> None:
    simulate.simulate(world, ORIGIN, end, simulate.Pace(seconds_per_day=0))


def _labels(data: Path) -> pd.DataFrame:
    return pd.concat(
        pd.read_parquet(p).assign(file=p.stem) for p in sorted((data / "labels").iterdir())
    )


def _files(data: Path) -> dict[str, bytes]:
    return {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}


def test_the_loop_releases_every_changed_label_once_never_before_its_booking(
    loaded: pd.DataFrame, tmp_path: Path
) -> None:
    world = _world(loaded, tmp_path)

    _play(world)

    booked = pd.concat(pd.read_parquet(p) for p in (world.data / "bookings").iterdir())
    labels = _labels(world.data)
    assert sorted(labels["booking_id"]) == sorted(booked["booking_id"]) == list(range(len(loaded)))
    joined = labels.merge(booked[["booking_id", "booking_date"]], on="booking_id")
    assert (joined["label_date"] >= joined["booking_date"]).all()
    assert joined["file"].tolist() == [str(d) for d in joined["label_date"]]
    # The files carry the changed outcomes and release dates, not the source's.
    expected = _rule().relabel(loaded, ORIGIN).set_index("booking_id").sort_index()
    written = labels.set_index("booking_id").sort_index()
    assert written["is_canceled"].tolist() == expected["is_canceled"].tolist()
    assert written["label_date"].tolist() == expected["label_date"].dt.date.tolist()
    assert (written["is_canceled"] != loaded.set_index("booking_id")["is_canceled"]).sum() > 100


def test_a_resumed_run_writes_the_same_labels_as_one_run(
    loaded: pd.DataFrame, tmp_path: Path
) -> None:
    whole = _world(loaded, tmp_path / "whole")
    _play(whole)
    resumed = _world(loaded, tmp_path / "resumed")

    _play(resumed, end=date(2016, 3, 15))
    _play(resumed)

    assert _files(resumed.data) == _files(whole.data)


def test_the_answer_key_has_the_entry_and_the_data_folder_does_not(
    loaded: pd.DataFrame, tmp_path: Path
) -> None:
    world = _world(loaded, tmp_path)

    _play(world, end=START)

    assert answer_key.lines(world.folder) == [
        {
            "kind": "model_decay",
            "category": "model_performance",
            "column": "market_segment,deposit_type,lead_time",
            "start": "2016-03-01",
            "end": "2016-04-30",
        }
    ]
    data = b"".join(_files(world.data).values()).lower()
    assert [w for w in ["model_decay", "model_performance", "decay"] if w.encode() in data] == []
    assert paths.labels_file(world.data, START).exists()
