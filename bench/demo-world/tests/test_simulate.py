import json
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from demo_world import answer_key, backfill, paths, scenarios, simulate, source
from tests.fixture import DEFAULT_SCENARIO, FIXTURE_CSV, FIXTURE_END, FIXTURE_START

FIXTURE_DAYS = [FIXTURE_START + timedelta(days=n) for n in range(5)]
# The fixture's last release date: every booking of the fixture is labelled by then.
LAST_RELEASE = date(2017, 1, 1)
INSIDE = "start = 2016-01-02\nend = 2016-01-03\n"
INJECTIONS = {
    "null_spike": 'columns = ["country", "adr"]\nshare = 0.5\n',
    "schema_break": 'column = "lead_time"\nrename_to = "lead_days"\n',
    "out_of_range": 'column = "adr"\nfactor = 100.0\n',
    "score_failure": "",
    "ci_failure": "",
    "model_decay": 'group = { market_segment = "Online TA", deposit_type = "No Deposit" }\n'
    "max_share = 1.0\nramp_end = 2016-01-03\n",
}
# The plan whose "Planted flaws" list `answer_key.PLANTED_FLAWS` carries.
PLAN = Path(__file__).parents[3] / "docs" / "superpowers" / "plans" / "2026-10-27-demo-world.md"


@pytest.fixture(scope="module")
def loaded() -> pd.DataFrame:
    return source.load_source(FIXTURE_CSV)


class FakeTime:
    """A clock that moves only when the loop sleeps, recording every sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


FAST = simulate.Pace(seconds_per_day=0)


def _world(
    loaded: pd.DataFrame, root: Path | None = None, text: str | None = None
) -> simulate.World:
    data = paths.data_dir() if root is None else root / "data"
    folder = paths.world_dir() if root is None else root / "world"
    scenario = scenarios.NO_SCENARIO
    if text is not None:
        folder.parent.mkdir(parents=True, exist_ok=True)
        (folder.parent / "scenario.toml").write_text(text, encoding="utf-8")
        scenario = scenarios.load(folder.parent / "scenario.toml")
    return simulate.World(loaded=loaded, data=data, folder=folder, scenario=scenario)


def _run(
    world: simulate.World, start: date = FIXTURE_START, end: date = FIXTURE_END
) -> simulate.Summary:
    return simulate.simulate(world, start, end, FAST)


def _files(data: Path) -> dict[str, bytes]:
    return {p.relative_to(data).as_posix(): p.read_bytes() for p in data.rglob("*") if p.is_file()}


def _names(folder: Path) -> list[str]:
    return sorted(p.stem for p in folder.iterdir())


def _last_done(world: simulate.World) -> str:
    return json.loads((world.folder / "state.json").read_text(encoding="utf-8"))["last_done"]


def test_five_fast_days_write_each_days_bookings_and_labels(loaded: pd.DataFrame) -> None:
    world = _world(loaded)

    summary = _run(world)

    days = [day.isoformat() for day in FIXTURE_DAYS]
    assert _names(world.data / "bookings") == days
    assert _names(world.data / "labels") == days
    day_three = source.bookings_frame(loaded, date(2016, 1, 3))
    assert pq.read_table(paths.bookings_file(world.data, date(2016, 1, 3))).equals(
        pa.Table.from_pandas(day_three, schema=source.BOOKINGS_SCHEMA, preserve_index=False)
    )
    labels = pq.read_table(paths.labels_file(world.data, FIXTURE_START))
    assert labels.column("booking_id").to_pylist() == [9, 11, 12]
    assert pq.read_schema(paths.bookings_file(world.data, FIXTURE_START)) == source.BOOKINGS_SCHEMA
    assert summary == simulate.Summary(first=FIXTURE_START, last=FIXTURE_END, days=5)
    assert _last_done(world) == "2016-01-05"


def test_a_day_without_rows_writes_no_file_but_is_done(loaded: pd.DataFrame) -> None:
    world = _world(loaded)

    _run(world, end=date(2016, 1, 7))

    assert not paths.bookings_file(world.data, date(2016, 1, 7)).exists()
    assert _last_done(world) == "2016-01-07"


def test_every_written_booking_is_labelled_once_never_before_its_booking(
    loaded: pd.DataFrame,
) -> None:
    world = _world(loaded)
    backfill.backfill(loaded, world.data, FIXTURE_START)

    _run(world, end=LAST_RELEASE)

    booked = pd.concat(pd.read_parquet(p) for p in (world.data / "bookings").iterdir())
    labels = pd.concat(
        pd.read_parquet(p).assign(file=p.stem) for p in (world.data / "labels").iterdir()
    )
    assert sorted(labels["booking_id"]) == sorted(booked["booking_id"]) == list(range(21))
    joined = labels.merge(booked[["booking_id", "booking_date"]], on="booking_id")
    assert bool((joined["label_date"] >= joined["booking_date"]).all())
    assert joined["file"].tolist() == [str(d) for d in joined["label_date"]]


def test_a_restart_resumes_after_the_last_day_done_without_rewriting(
    loaded: pd.DataFrame,
) -> None:
    world = _world(loaded)
    _run(world, end=date(2016, 1, 3))
    done = [
        paths.bookings_file(world.data, date(2016, 1, 2)),
        paths.labels_file(world.data, date(2016, 1, 3)),
    ]
    for path in done:
        path.write_bytes(b"sentinel")

    summary = _run(world)

    assert all(path.read_bytes() == b"sentinel" for path in done)
    assert _names(world.data / "bookings") == [day.isoformat() for day in FIXTURE_DAYS]
    assert summary == simulate.Summary(first=date(2016, 1, 4), last=FIXTURE_END, days=2)
    assert _last_done(world) == "2016-01-05"


def test_a_restart_with_nothing_left_writes_nothing(loaded: pd.DataFrame) -> None:
    world = _world(loaded)
    _run(world)
    before = _files(world.data)

    assert _run(world) == simulate.Summary(first=None, last=None, days=0)
    assert _files(world.data) == before


def test_a_start_after_the_next_day_due_is_refused(loaded: pd.DataFrame) -> None:
    world = _world(loaded)
    _run(world, end=date(2016, 1, 2))

    with pytest.raises(simulate.SimulateError, match="2016-01-03"):
        _run(world, start=date(2016, 1, 4))

    assert _last_done(world) == "2016-01-02"


def test_an_end_before_the_start_is_refused(loaded: pd.DataFrame) -> None:
    with pytest.raises(simulate.SimulateError, match="before"):
        _run(_world(loaded), start=FIXTURE_END, end=FIXTURE_START)


def test_a_crash_between_a_days_two_files_reruns_that_day(
    loaded: pd.DataFrame, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean = _world(loaded, tmp_path / "clean")
    _run(clean)
    world = _world(loaded)
    crash_at = paths.labels_file(world.data, date(2016, 1, 3))
    write = source.write_frame

    def crash_once(frame: pd.DataFrame, schema: pa.Schema, path: Path) -> None:
        if path == crash_at:
            monkeypatch.setattr(source, "write_frame", write)
            raise OSError("disk unplugged")
        write(frame, schema, path)

    monkeypatch.setattr(source, "write_frame", crash_once)
    with pytest.raises(OSError, match="disk unplugged"):
        _run(world)
    assert _last_done(world) == "2016-01-02"
    assert paths.bookings_file(world.data, date(2016, 1, 3)).exists()

    _run(world)

    assert _files(world.data) == _files(clean.data)
    assert _last_done(world) == "2016-01-05"


@pytest.mark.parametrize("kind", scenarios.INJECTED_KINDS)
def test_each_injected_kind_changes_exactly_its_files_and_dates(
    loaded: pd.DataFrame, tmp_path: Path, kind: str
) -> None:
    clean = _world(loaded, tmp_path / "clean")
    _run(clean)
    text = f'[[injection]]\nkind = "{kind}"\n{INJECTIONS[kind]}{INSIDE}'
    world = _world(loaded, tmp_path / "injected", text)
    rerun = _world(loaded, tmp_path / "rerun", text)

    _run(world)
    _run(rerun)

    injected, expected = _files(world.data), _files(clean.data)
    changed = sorted(name for name in expected if injected[name] != expected[name])
    file_kind = kind in ("null_spike", "schema_break", "out_of_range")
    assert changed == (
        ["bookings/2016-01-02.parquet", "bookings/2016-01-03.parquet"] if file_kind else []
    )
    assert injected.keys() == expected.keys()
    assert _files(rerun.data) == injected


def test_injected_files_are_readable_with_the_broken_schema(
    loaded: pd.DataFrame, tmp_path: Path
) -> None:
    spike = '[[injection]]\nkind = "null_spike"\n' + INJECTIONS["null_spike"] + INSIDE
    world = _world(loaded, tmp_path, spike)

    _run(world)

    table = pq.read_table(paths.bookings_file(world.data, date(2016, 1, 2)))
    assert table.schema.field("adr").nullable
    assert table.column("adr").null_count == 1
    assert table.column("country").null_count == 1


def _default_world(loaded: pd.DataFrame) -> simulate.World:
    world = _world(loaded)
    return simulate.World(
        loaded=loaded,
        data=world.data,
        folder=world.folder,
        scenario=scenarios.load(DEFAULT_SCENARIO),
    )


def _key(world: simulate.World) -> list[answer_key.Record]:
    lines = (world.folder / "answer_key.jsonl").read_text(encoding="utf-8").splitlines()
    return [answer_key.Record(**json.loads(line)) for line in lines]


def test_the_answer_key_lists_every_injection_and_natural_shift_once(
    loaded: pd.DataFrame,
) -> None:
    world = _default_world(loaded)

    _run(world, end=date(2016, 12, 31))

    expected = [r for p in world.scenario.problems for r in p.records()]
    assert sorted(_key(world), key=str) == sorted(expected, key=str)
    assert {r.kind for r in _key(world)} == {*scenarios.INJECTED_KINDS, "natural_shift"}
    assert ("natural_shift", "drift", "market_segment") in {
        (r.kind, r.category, r.column) for r in _key(world)
    }


def test_the_answer_key_holds_only_problems_the_run_has_reached(loaded: pd.DataFrame) -> None:
    world = _default_world(loaded)

    _run(world, end=date(2016, 8, 1))

    assert _key(world) == [
        answer_key.Record("null_spike", "data_quality", "country", "2016-08-01", "2016-08-07"),
        answer_key.Record("null_spike", "data_quality", "adr", "2016-08-01", "2016-08-07"),
    ]


def test_restarting_twice_leaves_the_answer_key_unchanged(loaded: pd.DataFrame) -> None:
    world = _default_world(loaded)
    _run(world, end=date(2016, 8, 6))
    key = world.folder / "answer_key.jsonl"
    before = key.read_bytes()

    (world.folder / "state.json").unlink()
    _run(world, end=date(2016, 8, 6))
    (world.folder / "state.json").unlink()
    _run(world, end=date(2016, 8, 6))

    assert key.read_bytes() == before


def test_a_restart_keeps_answer_key_lines_of_another_shape(loaded: pd.DataFrame) -> None:
    world = _default_world(loaded)
    _run(world, end=date(2016, 2, 15))
    key = world.folder / "answer_key.jsonl"
    # Other parts of the world (a PR merge, a CI window) record lines of their own shape.
    with key.open("a", encoding="utf-8", newline="\n") as file:
        file.write('{"kind": "pr_merged", "pr": 7, "delay_days": 3}\n')
    before = key.read_bytes()

    (world.folder / "state.json").unlink()
    _run(world, end=date(2016, 2, 15))

    assert key.read_bytes() == before


def test_the_planted_flaws_are_written_to_the_world_folder(loaded: pd.DataFrame) -> None:
    world = _world(loaded)

    _run(world)

    written = json.loads((world.folder / "planted_flaws.json").read_text(encoding="utf-8"))
    assert len(written) == 7
    assert [flaw["id"] for flaw in written] == [flaw.id for flaw in answer_key.PLANTED_FLAWS]
    assert all(flaw.keys() == {"id", "category", "where", "what"} for flaw in written)
    assert "0.95" in written[0]["what"]


def test_the_planted_flaws_match_the_plans_list() -> None:
    # The plan's item after its bold title, code marks dropped. Flaw 7's text predates this and
    # words the plan's sentence differently, so only its place in the list is checked.
    items = re.findall(r"^(\d)\. \*\*.+?\*\* (.+)$", PLAN.read_text(encoding="utf-8"), re.M)
    texts = [text.replace("`", "") for _, text in items]

    assert [number for number, _ in items] == [str(n) for n in range(1, 8)]
    assert [flaw.what for flaw in answer_key.PLANTED_FLAWS[:6]] == texts[:6]
    assert "PR-AUC 0.95" in answer_key.PLANTED_FLAWS[0].what


def test_nothing_in_the_data_folder_mentions_the_answer_key_or_the_flaws(
    loaded: pd.DataFrame,
) -> None:
    one_day = [kind for kind in INJECTIONS if kind != "model_decay"]
    text = "".join(
        f'[[injection]]\nkind = "{kind}"\n{INJECTIONS[kind]}start = {day}\nend = {day}\n'
        for kind, day in zip(one_day, FIXTURE_DAYS, strict=True)
    )
    text += (
        f'[[injection]]\nkind = "model_decay"\n{INJECTIONS["model_decay"]}'
        f"start = {FIXTURE_START}\nend = {FIXTURE_END}\n"
    )
    world = _world(loaded, text=text)

    _run(world)

    data = b"".join(_files(world.data).values()).lower()
    words = ["answer_key", "planted", "natural_shift", "injection", *scenarios.INJECTED_KINDS]
    words += [flaw.id for flaw in answer_key.PLANTED_FLAWS]
    words += [flaw.what[:40] for flaw in answer_key.PLANTED_FLAWS]
    assert [word for word in words if word.lower().encode() in data] == []
    assert {p.name for p in world.folder.iterdir()} == {
        "answer_key.jsonl",
        "planted_flaws.json",
        "state.json",
    }


def test_a_world_folder_inside_the_data_folder_is_refused(loaded: pd.DataFrame) -> None:
    world = _world(loaded)
    inside = simulate.World(loaded=loaded, data=world.data, folder=world.data / "world")

    with pytest.raises(simulate.SimulateError, match="inside the data folder"):
        _run(inside)

    assert not world.data.exists()


def test_the_loop_paces_each_day_to_the_seconds_per_day(loaded: pd.DataFrame) -> None:
    fake = FakeTime()

    simulate.simulate(
        _world(loaded),
        FIXTURE_START,
        FIXTURE_END,
        simulate.Pace(seconds_per_day=120, clock=fake.clock, sleep=fake.sleep),
    )

    assert fake.sleeps == [120, 120, 120, 120]


def test_zero_seconds_per_day_never_sleeps(loaded: pd.DataFrame) -> None:
    fake = FakeTime()

    simulate.simulate(
        _world(loaded),
        FIXTURE_START,
        FIXTURE_END,
        simulate.Pace(seconds_per_day=0, clock=fake.clock, sleep=fake.sleep),
    )

    assert fake.sleeps == []


def test_a_state_file_without_the_first_day_is_refused_by_name(loaded: pd.DataFrame) -> None:
    world = _world(loaded)
    world.folder.mkdir(parents=True)
    (world.folder / "state.json").write_text('{"last_done": "2016-01-02"}\n', encoding="utf-8")

    with pytest.raises(simulate.SimulateError, match=r"state.json"):
        _run(world)
