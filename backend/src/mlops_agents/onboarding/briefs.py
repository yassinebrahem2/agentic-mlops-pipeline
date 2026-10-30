"""What each audit agent is asked to do when a handoff is approved, and the message's cap."""

from __future__ import annotations

from typing import Final

KICKOFF_AGENTS: Final[tuple[str, ...]] = (
    "data-quality",
    "model-performance",
    "retraining",
    "deployment",
)

# The whole kickoff message (marker, summary and brief), in characters.
KICKOFF_MESSAGE_CAP: Final = 12_000
CUT_LINE: Final = "(cut: read the rest with ReadHandoff)"

# Each agent's own parts of the handoff, as keys of `handoff_preview.sections`: rendered first,
# so a cut message keeps them.
BRIEF_SECTIONS: Final[dict[str, tuple[str, ...]]] = {
    "data-quality": ("data_roles", "operations"),
    "model-performance": ("models",),
    "retraining": ("models",),
    "deployment": ("models",),
}

_HOW_TO_AUDIT: Final = (
    "Study your part first, then the rest of the handoff where it touches yours, with what you "
    "have: your context hooks, Bash with duckdb on the connections under /data, the tracking "
    "record, the repository and the notes. Save what you learn with SaveNote. Open pull "
    "requests or issues for fixes through your usual tools and their approvals. End with a "
    "report: what is good, what is broken and what is missing, each with its evidence.\n"
    "The summary above may be cut: use ReadHandoff for any part you need in full.\n"
    "If you audited an earlier handoff, re-audit what this one changes; the rest still stands."
)

AUDIT_BRIEFS: Final[dict[str, str]] = {
    "data-quality": (
        "Your audit: the data. Study first the data roles of every table (training, "
        "serving_inputs as the model's raw inputs as served, predictions, labels), their ID and "
        "time columns, and the feeds in operations with their cadence, grace and time zone. "
        "Check that each feed's data arrives as described and that the serving_inputs match "
        "what the model was trained on.\n" + _HOW_TO_AUDIT
    ),
    "model-performance": (
        "Your audit: the models' performance. Study first each model's profile (objective, "
        "primary metric and why, operating threshold, error costs, the groups to watch), its "
        "predictions and labels references, and its label delay and label maturity. Check that "
        "predictions and labels can be joined and the primary metric measured per group.\n"
        + _HOW_TO_AUDIT
    ),
    "retraining": (
        "Your audit: retraining. Study first each model's train and evaluate commands, the "
        "entry files they run and their placeholders, and its promotion mode (the team's gate "
        "or a person). Check that the commands match the repository and that a new version "
        "can be trained and evaluated as described.\n" + _HOW_TO_AUDIT
    ),
    "deployment": (
        "Your audit: deployment and rollback. Study first each model's deploy and rollback "
        "commands, its environments (tracker alias to environment), its production signal and "
        "its promotion mode. Check that a deploy is visible through the production signal and "
        "that a rollback path exists for every model in production.\n" + _HOW_TO_AUDIT
    ),
}
