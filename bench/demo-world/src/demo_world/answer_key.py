"""The evaluation's ground truth: the answer key and the planted flaws.

Both live in the world's folder only; nothing here is ever written into the data folder,
which the company and the agents read.
"""

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

ANSWER_KEY_FILE = "answer_key.jsonl"
PLANTED_FLAWS_FILE = "planted_flaws.json"

# The incident categories of the roadmap and the stage 3 spec, so scoring maps one to one.
type Category = Literal["data_quality", "drift", "model_performance", "operational"]


@dataclass(frozen=True)
class Record:
    """One answer-key line: a problem in one column (or in none) over its business dates."""

    kind: str
    category: Category
    column: str | None
    start: str
    end: str


@dataclass(frozen=True)
class Flaw:
    """A flaw planted in the company's setup by design, for the agents' first audit to find."""

    id: str
    category: Category
    where: str
    what: str


# The demo-world plan's "Planted flaws", in its order; the numbers are the plan's replay figures.
PLANTED_FLAWS: tuple[Flaw, ...] = (
    Flaw(
        "random_validation_split",
        "model_performance",
        "train and evaluate (src/hotel_demo/pipeline.py)",
        "evaluate scores a random 20% of the training window: PR-AUC about 0.98, "
        "against about 0.66 on later bookings.",
    ),
    Flaw(
        "accuracy_only_alert",
        "model_performance",
        "the accuracy alert (src/hotel_demo/checks/accuracy.py)",
        "Accuracy stays between 0.66 and 0.78 while PR-AUC decays (about 0.74 in January 2016 "
        "to 0.45 in October), because about 65% of bookings are never cancelled.",
    ),
    Flaw(
        "no_calibration_check",
        "model_performance",
        "the legacy monitoring (src/hotel_demo/checks/)",
        "The model predicts a mean cancellation chance of about 0.15 against a real rate of "
        "about 0.35, which is what overbooking depends on.",
    ),
    Flaw(
        "manual_retraining_only",
        "operational",
        "the production schedule (ops/schedule.toml)",
        "Retraining is by hand only: nothing schedules it, so the decay builds up.",
    ),
    Flaw(
        "drift_check_blind_to_categories",
        "drift",
        "the drift report (src/hotel_demo/checks/drift.py)",
        "The drift report looks at four numeric columns only, so the late-2016 shift "
        "(non-refundable group bookings, in deposit_type and market_segment) goes unseen.",
    ),
    Flaw(
        "training_uses_final_booking_changes",
        "model_performance",
        "train (src/hotel_demo/pipeline.py)",
        "train takes booking_changes from the labels (its final value), while bookings are "
        "scored with 0. On this data it costs little (PR-AUC 0.661 against 0.665): a finding, "
        "not a failure.",
    ),
    Flaw(
        "mlflow_without_authentication",
        "operational",
        "the MLflow tracking server (started by the world)",
        "Anyone who can reach the tracking server can change the production model; the agents "
        "should report it and propose enabling MLflow's authentication. Scored on the audit "
        "report only: the world starts the server, so a PR to the company cannot close it.",
    ),
)


def _present(path: Path) -> set[Record]:
    if not path.exists():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {Record(**json.loads(line)) for line in lines}


def append_new(world: Path, records: Iterable[Record]) -> None:
    """Append the records the key does not hold yet, so a restart or a rerun adds no line."""
    path = world / ANSWER_KEY_FILE
    present = _present(path)
    new = [record for record in dict.fromkeys(records) if record not in present]
    if not new:
        return
    world.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write("".join(json.dumps(asdict(record)) + "\n" for record in new))


def write_planted_flaws(world: Path) -> None:
    """Write the planted flaws list, replacing any earlier copy."""
    world.mkdir(parents=True, exist_ok=True)
    text = json.dumps([asdict(flaw) for flaw in PLANTED_FLAWS], indent=2) + "\n"
    (world / PLANTED_FLAWS_FILE).write_text(text, encoding="utf-8", newline="\n")
